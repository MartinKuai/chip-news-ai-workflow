# Daily Chip News 架构

## Runtime flow

一次日报运行包含来源收集、逐篇 AI Graph 和确定性发布三个阶段：

```text
RSS feeds
  ↓ collect + deduplicate
Article candidates
  ↓ extraction + deterministic cleaning (no model call)
Writer (compose)
  ├─ SKIP ────────────────────────────────→ END
  ↓ KEEP + Draft
Reviewer
  ├─ PASS ─────────→ Publisher ──────────→ END
  ├─ REJECT + revision_count < limit ────→ Writer (revise)
  └─ REJECT + revision_count >= limit ───→ HOLD → END
  └─ API failure ────────────────────────→ article-level failure
```

当前运行时依赖 LangGraph、Gemini API、RSS/Jina Reader 和 Telegram，无需额外持久化组件。

## Source collection

`collect_articles()` 依次解析配置中的 RSS feeds，并返回 `SourceCollectionResult`：

```python
SourceCollectionResult(
    articles=[...],
    sources_total=5,
    sources_ok=4,
    sources_failed=1,
    failures=[...],
)
```

每个 feed 独立处理：

- 解析成功时提取当前 feed 的 entries，并按 URL 去重。
- `bozo=True` 但仍有 entries 时保留这些 entries。
- 解析异常，或 `bozo=True` 且没有 entries 时记录 `SourceFailure`，继续后续 feed。
- 至少一个来源可用时，将已收集 candidates 交给图处理。
- 所有来源均不可用时抛出 `SourceCollectionError`，输出失败 summary、发送告警，并得到 `FAILED` 结果（exit 1）。

来源失败记录只包含来源 URL / 名称和异常类型。

### 确定性清洗

`clean_extracted_text(raw, max_chars)` 在调用模型之前运行，不产生 Gemini 请求：

- 去掉 Jina Reader 的 `Title:` / `URL Source:` / `Published Time:` / `Markdown Content:` 元数据块；
- 去掉纯图片行、Markdown 分隔线和连续重复行；
- 折叠多余空行；
- 超过 `ARTICLE_CONTENT_CHARS` 时优先在段落边界截断并追加 `[content truncated]`。

原则是高信息密度，而不是单纯的大上下文。

## Node responsibility

### Writer

一次调用同时完成相关性判断、事实提取和中文成稿，因此正常情况下每篇只需要一次 Writer 调用：

```text
compose:
  article metadata
+ cleaned source text
+ editorial scope / brief
+ output schema
→ decision KEEP/SKIP + Research Notes + Draft

revise:
  article metadata
+ Research Notes（首轮产出）
+ previous Draft
+ Revision Brief（仅 Reviewer 提供）
+ editorial brief / output schema
→ 更新后的 Draft
```

`revise` 不重新抓取正文、不新增事实，也不接收 Reviewer 的完整输出，只接收 `revision_brief`。返工后 Research Notes 由程序确定性保留首轮结果，模型无法在返工中改变事实基础。

### Reviewer

Reviewer 接收 Research Notes、Draft、Rubric 和输出结构。Rubric 覆盖 factual grounding、source support、unsupported claims、commercial relevance、recency、clarity、duplication、tone、length 和 format。输出为机器可读的 `PASS` / `REJECT`、评分、问题和 Revision Brief。

Reviewer 与 API 基础设施失败严格分离：语义拒绝是内容判断（`HOLD`/返工），API 失败是文章级错误。

### Publisher

Publisher 为确定性发布层。图状态与 `review.status` 同时为 `PASS` 时发送 Telegram。Markdown 请求返回 400 时尝试一次纯文本 fallback；最终发送失败抛出 `PublisherError`。同一个 Publisher 还提供不带原文链接的 `publish_alert`，供 `FAILED` / `PARTIAL_SUCCESS` 发送运维告警；该路径不调用 Gemini。

## Graph State

每篇文章维护以下状态：

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

原始正文只存在于 Writer `compose` 调用的局部 payload，不写入 Graph State。

## Data contracts

### Writer output（合并后的单一 JSON）

```json
{
  "decision": "KEEP",
  "reason": "...",
  "topic": "...",
  "notes": [
    {
      "claim": "...",
      "evidence": "...",
      "why_it_matters": "...",
      "confidence": 0.9
    }
  ],
  "draft": {
    "headline": "...",
    "summary": "...",
    "key_facts": ["..."],
    "why_it_matters": "...",
    "telegram_copy": "..."
  }
}
```

`source`、`url`、`published_at` 由确定性代码从候选元数据注入，不由模型生成。`decision=SKIP` 时忽略笔记内容，忽略 Draft。

### Review

```json
{
  "status": "REJECT",
  "scores": {"factuality": 9, "relevance": 8, "clarity": 9},
  "issues": [{"severity": "major", "problem": "..."}],
  "revision_brief": ["..."]
}
```

AI 输出先经过 `responseSchema` 约束，再经过本地 schema validator。缺字段、非法枚举、无效分数或非 JSON 对象会作为当前文章失败记录。

## Review routing

`revision_count` 表示已使用的返工次数。默认 `MAX_REVISIONS=1`：第一次 REJECT 后计数为 1 并返回 Writer；再次 REJECT 直接进入 `HOLD`。`MAX_REVISIONS=0` 时，初稿被拒后立即 HOLD。循环存在硬上限，不存在无限 Writer <-> Reviewer 往返。

终止状态：

- `SKIP`：内容不在编辑范围内，不发布。
- `PASS`：审核通过且 Publisher 完成发送。
- `HOLD`：达到返工上限，不发布（内容级淘汰，不是系统失败）。
- 失败：当前文章处理失败，记录 category 后继续下一篇。

## Failure semantics

### Article level

图节点异常包装为带安全阶段信息的 `NodeExecutionError`：`writer`、`reviewer` 或 `publisher`。正文提取、JSON 解析、schema 校验和单篇瞬态请求失败计入 `failed`，后续文章继续运行。

### Error taxonomy

`errors.classify_failure(stage, cause)` 把异常映射为一个类别和三个路由标记（`transient`、`stop_run`、`force_failed`）：

```text
TRANSIENT_RATE_LIMIT      429
TRANSIENT_SERVER          5xx
TRANSIENT_NETWORK         timeout / connection reset / transport error
MODEL_RESPONSE_INVALID    Gemini 输出不是合法 JSON 对象
MODEL_RESPONSE_TRUNCATED  finishReason=MAX_TOKENS
SCHEMA_INVALID            本地 schema 校验失败
SOURCE_ERROR              正文提取失败
PUBLISH_ERROR             Telegram 发送失败
CONFIG_ERROR              Gemini 认证 / 权限 / 模型配置错误（停止处理）
UNEXPECTED_ERROR          程序级异常（始终 FAILED）
```

### Gemini service health（rolling window）

取代旧的"连续两次失败"熔断：

- 窗口大小 `GEMINI_HEALTH_WINDOW=5`，阈值 `GEMINI_HEALTH_THRESHOLD=3`。
- 事件粒度 = logical Gemini call：每次 Writer/Reviewer 调用（含 revision）由节点记录恰好一个事件，成功 SUCCESS / 瞬态 TRANSIENT / 响应或 schema 问题 NON_TRANSIENT，成功与失败同粒度。
- 只有 `writer` / `reviewer` 两个 AI 阶段的瞬态失败计入阈值。
- `MODEL_RESPONSE_INVALID` / `MODEL_RESPONSE_TRUNCATED` / `SCHEMA_INVALID` 占窗口位置但不计瞬态，也不清空历史。
- `SOURCE_ERROR`、`PUBLISH_ERROR` 不进入窗口。
- 成功完成的文章记录为成功事件，帮助窗口恢复（旧失败被挤出）。
- 熔断打开时打印 `window=[...] transient=n/5 threshold=3` 以及最后一个失败的 stage/category，然后停止继续消耗 API。

### Run outcome

`outcomes.decide_run_outcome()` 只输出四种结果：

| Outcome | 条件 | exit code |
| --- | --- | --- |
| `SUCCESS` | run 正常结束、`published > 0` 且无文章失败 | 0 |
| `PARTIAL_SUCCESS` | `published > 0`，但存在文章失败 / breaker 打开 / 预算耗尽 | 0 |
| `EMPTY_SUCCESS` | 没有候选文章，或候选全部 `SKIP`/`HOLD` | 0 |
| `FAILED` | `published == 0` 且存在失败、breaker 或程序级故障 | 1 |

`force_failed` 只用于程序级故障（UNEXPECTED_ERROR、非法终态、全源失败），因此"已经发布过文章"的运行不会被后续 Gemini 故障重新标红；同时 `published == 0` 的真实系统故障不会被伪装成成功。

### Run budget

`RUN_BUDGET_SECONDS=2100` 是整场 run 的处理预算：每次取下一篇之前检查，超时则停止处理并在 summary 中打印 `Run budget exceeded`。同一条 deadline 也作为硬上限传给 `GeminiClient`：单次调用的 retry deadline 取 `min(call_budget, run_deadline)`，每次 HTTP 请求的 timeout 会被 clamp 到剩余预算，因此即使单篇文章卡在故障上也不会拖过 job `timeout-minutes: 60`。

## Gemini client

`GeminiClient.request_json()` 负责全部网络与解析行为：

- **请求**：`x-goog-api-key` header（key 不出现在 URL）；`generationConfig.responseMimeType=application/json`；可选 `responseSchema`、`maxOutputTokens`、`thinkingConfig.thinkingLevel`。
- **超时**：`GEMINI_TIMEOUT_SECONDS=180` 单次 HTTP 超时；`GEMINI_CALL_BUDGET_SECONDS=240` 限制单次逻辑调用（含 retry）的总时长，deadline 到达即停止重试并抛出瞬态错误。
- **重试**：最多 `GEMINI_MAX_ATTEMPTS=5` 次。429 优先使用 `Retry-After`（上限 120s）；5xx / network / timeout 使用指数退避 2s→4s→8s→16s，带 jitter，单次 sleep 上限 30s。
- **日志**：每条 retry 记录 `purpose / attempt / http / retry_after / backoff / reason`，不输出 key、header 或请求体。
- **JSON 容错**：直接解析 → 提取 fenced json block → 最多一次 `REPAIR_INSTRUCTION` 修复调用（只修格式，`allow_repair=False` 防止递归）→ 仍失败抛 `GeminiResponseError`。截断单独抛 `GeminiTruncatedResponseError`，不做 repair。repair 与原调用共享同一个 logical-call deadline（`min(call_budget, run_deadline)`），不会重新获得完整预算。
- **thinking 兼容**：模型若以 400 明确拒绝 `thinkingConfig`，本次运行降级为模型默认值（不重复探测），并记录 `thinking_downgrades`。

## Model routing

两个节点分别绑定自己的配置项：

```text
Writer   ← WRITER_MODEL   + WRITER_THINKING_LEVEL / WRITER_MAX_OUTPUT_TOKENS
Reviewer ← REVIEWER_MODEL + REVIEWER_THINKING_LEVEL / REVIEWER_MAX_OUTPUT_TOKENS
```

Gemini client 每次调用都显式接收 `model`、role instruction、payload、`purpose`、output schema、thinking level 和 output token 上限。

## Metrics and summary

`RunMetrics` 记录无法从失败列表推导的计数：Gemini 请求数、成功数、retry 数、429/5xx/network 次数、response invalid/truncated、thinking 降级、JSON repair 次数与成功率。

`app._print_summary()` 输出 `run_outcome` / `exit_code` / 兼容旧字段的 `Run summary`，以及 `Sources` / `Articles` / `Gemini` / `Writer` / `Reviewer` 分组，使一次失败运行可以直接从日志回答：是 source 还是 Gemini、429 还是 503、哪一步失败、retry 几次、等了多久、breaker 为什么打开、已经发布几篇、最终为何是 SUCCESS/PARTIAL/FAILED。

## Secret boundary

- 本地凭证存放在 Git 忽略的 `.env` 或系统环境变量中。
- GitHub Actions 从 Repository Secrets 读取 Gemini 和 Telegram 凭证。
- 模型名称从 Repository Variables 读取。
- Gemini key 通过 `x-goog-api-key` header 发送。
- summary、失败记录和 retry 日志只输出安全字段、异常类型与类别。
