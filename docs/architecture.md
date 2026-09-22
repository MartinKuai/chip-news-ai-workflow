# Daily Chip News 架构

## Runtime flow

```text
RSS feeds
  ↓ collect + deduplicate (sources.py)
Article candidates
  ↓ deterministic candidate control: canonical dedupe, junk filter,
    round-robin + recency, MAX_CANDIDATES_PER_RUN
Researcher (唯一读取 cleaned 正文的 AI 节点)
  ├─ SKIP ────────────────────────────────→ END
  ↓ KEEP + Research Notes
Writer (只读 notes + brief + schema)
  ↓ Draft
Reviewer (只读 notes + draft + rubric)
  ├─ PASS ─────────→ Publisher ──────────→ END
  ├─ REJECT + revision_count < limit ────→ Writer (revise: notes + previous_draft + brief)
  └─ REJECT + revision_count >= limit ───→ HOLD → END
  └─ API failure ────────────────────────→ article-level failure
```

运行时依赖 LangGraph、Gemini API、RSS/Jina Reader、Telegram，以及 GitHub Actions artifact（成本账本）。无数据库、无新 SaaS。

## Source collection 与候选控制

`collect_articles()` 逐个解析 RSS（单源失败只记录 `SourceFailure`；全部失败抛 `SourceCollectionError`）。随后 `select_candidates()` 以 0 Gemini 成本完成：

- `canonical_url()`：去 fragment、去 `utm_*`/`fbclid`/`gclid` 等追踪参数、统一大小写与 `www.`，用于跨源去重。
- 明显垃圾过滤：广告/招聘/newsletter 关键词、导航类精确标题（home/contact/...）、过短标题（< 6 字符）、`/jobs/`、`/careers/` 等 URL。
- 组内按 `published_at` recency 排序，然后按来源 round-robin 取候选，直到 `MAX_CANDIDATES_PER_RUN`（默认 6）。

正文提取后 `clean_extracted_text()` 去掉 Jina reader 元数据块、纯图片行、分隔线与重复行，并按 `ARTICLE_CONTENT_CHARS`（默认 12000）在段落边界截断。

## Node responsibility

### Researcher

- 输入：article metadata + cleaned raw content + editorial scope + output schema。
- 输出：`decision`（KEEP/SKIP）、`reason`、`topic`、1-4 条 `notes`（claim/evidence/why_it_matters/confidence）。
- `source` / `url` / `published_at` 由确定性代码注入，不由模型生成。
- 是唯一读取正文的 AI 节点；Writer 与 Reviewer 的 payload 中永远没有正文。

### Writer

- compose：`editorial_brief` + `research_notes` + 空 `revision_brief` + `output_schema`。
- revise：额外携带 `previous_draft`，仍不携带正文。
- 不访问网页、不新增事实、不改变 Research Notes（节点不返回 notes）。

### Reviewer

- 输入：`research_notes` + `draft` + `rubric` + `output_schema`。
- 输出：`status`（PASS/REJECT）、`scores`、`issues`、`revision_brief`。
- 语义拒绝与基础设施失败严格分离。

### Publisher

确定性发送层，只在图状态与 `review.status` 同时为 PASS 时调用 Telegram；Markdown 400 时回退纯文本。同时提供不带原文链接的 `publish_alert`，用于 `FAILED` / `PARTIAL_SUCCESS` / `COST_GUARD_STOPPED` 运维告警；该路径不调用 Gemini。

## Graph State

```python
{
    "article": {...},
    "research_notes": {...},
    "draft": {...},
    "review": {...},
    "revision_brief": [],
    "revision_count": 0,
    "status": "NEW",
    "published": False,
}
```

正文只存在于 Researcher 调用的局部 payload。

## Data contracts

```json
// Research Notes
{"decision": "KEEP", "reason": "...", "topic": "...", "source": "...", "url": "...",
 "published_at": "...",
 "notes": [{"claim": "...", "evidence": "...", "why_it_matters": "...", "confidence": 0.9}]}

// Draft
{"headline": "...", "summary": "...", "key_facts": ["..."],
 "why_it_matters": "...", "telegram_copy": "..."}

// Review
{"status": "REJECT", "scores": {"factuality": 9, "relevance": 8, "clarity": 9},
 "issues": [{"severity": "major", "problem": "..."}], "revision_brief": ["..."]}
```

AI 输出先经过 `responseSchema` 约束，再经过本地 validator；缺字段、非法枚举或非 JSON 对象记为当前文章失败。

## Review routing

`revision_count` 表示已使用的返工次数。默认 `MAX_REVISIONS=1`：第一次 REJECT 后计数为 1 并回到 Writer；再次 REJECT 直接 `HOLD`。循环有硬上限，且返工不会重新调用 Researcher。

## Failure semantics

### Error taxonomy

`errors.classify_failure(stage, cause)` 输出类别 + `transient` / `stop_run` / `force_failed`：

```text
TRANSIENT_RATE_LIMIT / TRANSIENT_SERVER / TRANSIENT_NETWORK   计入熔断
MODEL_RESPONSE_INVALID / MODEL_RESPONSE_TRUNCATED / SCHEMA_INVALID   文章级
SOURCE_ERROR / PUBLISH_ERROR / CONFIG_ERROR / COST_GUARD / UNEXPECTED_ERROR
```

### Gemini service health（rolling window）

- 窗口 `GEMINI_HEALTH_WINDOW=5`，阈值 `GEMINI_HEALTH_THRESHOLD=3`。
- 粒度 = logical call：节点为每次 Researcher/Writer/Reviewer 调用（含 revision）记录恰好一个事件。
- 瞬态失败计入阈值；`NON_TRANSIENT`（response/schema）占位但不计；`SOURCE_ERROR`、`COST_GUARD` 不进入窗口。
- 成功事件帮助窗口恢复；打开时打印 `window=[...] transient=n/5` 与最后失败的 stage/category。

### Run outcome

| Outcome | 条件 | exit |
| --- | --- | --- |
| `SUCCESS` | 正常完成、`published > 0`、无失败 | 0 |
| `PARTIAL_SUCCESS` | `published > 0` 且存在失败 / breaker / 时间预算耗尽 | 0 |
| `EMPTY_SUCCESS` | 无候选或全部 `SKIP`/`HOLD` | 0 |
| `COST_GUARD_STOPPED` | 成本保护主动停止 | 0 |
| `FAILED` | `published == 0` 且存在失败/服务不可用；或程序级故障 | 1 |

`force_failed` 只用于程序级故障（UNEXPECTED_ERROR、非法终态、全源失败）；`COST_GUARD_STOPPED` 是预期运营状态，不标红且不写 health window。

## Cost lifecycle

```text
[workflow] Prepare cost ledger
  1) 读取上一版 artifact（排除本次 run_id）；损坏/读取失败 → fail closed
  2) 无历史 → 初始化并打印 ledger initialized
  3) 写入 run_open 预留（整场 run 预算）并保存
[workflow] upload-artifact（run_open 先落地，崩溃也不低估）
[app] 每个 logical call（含 retry 的每次 attempt）
  4) countTokens（等价输入）→ 失败即拒绝
  5) lookup_price(model, now) → 未知/过期即拒绝
  6) projected = 输入×1.05×输入价 + max_output_tokens×输出价
  7) run_spend + projected ≤ run_budget 且 rolling + projected ≤ 7.50
  8) reserve → POST → 200: 按 usageMetadata 结算为 actual
        非 200（明确未生成）: release
        timeout / network 不确定: 保留 reservation
  9) JSON repair 走完整 4-8，作为独立 billable request
[app] run 结束：run_open 结算为实际 run 花费，保存账本
[workflow] upload-artifact if: always()
```

账本为 append-only JSONL（`entry_id` 后写覆盖前写）；rolling 30 天 = `consumed.actual` + `reserved/unresolved.projected`；保留 45 天。`COST_LEDGER_REQUIRED=1` 时缺失账本即 fail closed。

## Gemini client

- 请求：`x-goog-api-key` header；`responseMimeType=application/json`；可选 `responseSchema`、`maxOutputTokens`、`thinkingConfig.thinkingLevel`。
- 超时与预算：`GEMINI_TIMEOUT_SECONDS=180`；单次逻辑调用 `GEMINI_CALL_BUDGET_SECONDS=240`；整场 `RUN_BUDGET_SECONDS=2100`；HTTP timeout clamp 到剩余预算。
- 重试：最多 `GEMINI_MAX_ATTEMPTS=5`；429 优先 `Retry-After`（上限 120s）；5xx/network/timeout 指数退避 2/4/8/16s + jitter，单次 sleep ≤ 30s。
- 日志：`purpose / attempt / http / retry_after / backoff / reason`，不含 key、header、请求体。
- JSON 容错：直接解析 → fenced 提取 → 一次只修格式的 repair（与原调用共享 deadline）→ 仍失败抛 `GeminiResponseError`；截断抛 `GeminiTruncatedResponseError`。
- thinking 兼容：模型 400 明确拒绝 `thinkingConfig` 时本次运行降级为默认值并记录 `thinking_downgrades`。

## Metrics and summary

`RunMetrics` 记录请求/成功/retry/429/5xx/network、response invalid/truncated、thinking 降级、JSON repair、以及按 `purpose` 累计的 token 与成本。`RunSummary` 输出 `Run summary`、`Articles`（discovered/selected/eligible/...）、`Gemini`、`Gemini Cost`（tokens、billable requests、estimated cost、run/rolling 预算与剩余、cost guard 状态）、`Researcher`/`Writer`/`Reviewer` 明细。

## Secret boundary

- 本地凭证在 Git 忽略的 `.env`；CI 从 Repository Secrets 读取；模型名来自 Repository Variables。
- Gemini key 只经 `x-goog-api-key` 发送；summary、失败记录与 retry 日志只输出安全字段。
- 账本只保存 token 数与成本，不保存 prompt 内容。
