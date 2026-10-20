# Daily Chip News 架构

## Runtime flow

```text
RSS feeds
  ↓ sources.collect_articles()：ingestion → normalize → metadata
Candidates
  ↓ sources.select_candidates()：canonical dedupe → garbage filter
    → recency filter → source round-robin → MAX_CANDIDATES_PER_RUN
候选级循环（runner.run_daily，每个候选独立）
  ↓
Researcher（唯一读取正文的 AI 节点）
  ├─ SKIP ────────────────────────────────→ SKIPPED
  ↓ KEEP + Research Notes
Writer（compose：notes + brief + schema）
  ↓ Draft
Reviewer（notes + draft + rubric）
  ├─ PASS ──────────────→ Publisher ──────→ PUBLISHED
  ├─ REVISE + 未达上限 ─→ Writer（revise：notes + previous_draft + brief）
  ├─ REVISE + 达到上限 ─→ HOLD ───────────→ SKIPPED
  └─ REJECT ──────────────────────────────→ SKIPPED
  ↓ 任意节点异常
FAILED（候选级，不影响其他候选）
  ↓ 全部候选处理结束
Website：所有 PUBLISHED 条目 + run outcome → 网站仓库一个 commit
  （data/news/YYYY-MM-DD.json 仅在有条目时写；data/news/stats.json 每轮都写）
  ↓
Run Summary → Notification
```

运行时依赖：LangGraph、Gemini API、RSS / Jina Reader、GitHub REST API（写网站仓库、读本仓库定时运行历史）；Telegram 仅在 `PUBLISH_TELEGRAM=1` 时使用。无数据库、无额外 SaaS；模型每日请求预留与冷却由 GitHub Actions quota ledger artifact 跨 run 保存，运行次数统计由网站仓库里的 `stats.json` 跨 run 保存。

## Stage 1 — Sources 与 Candidate Selection

`collect_articles(articles_per_feed, feeds=SOURCE_FEEDS)` 逐源解析 RSS：

- 单源失败（解析异常、bozo、HTTP ≥ 400）只记录 `SourceFailure`，不影响其他来源。
- 所有来源均不可用 → `SourceCollectionError`，run 直接 `FAILED`。
- 每条候选为 `Candidate`：`id`（canonical URL 的 blake2s 短哈希）、`title`、`url`、`source`、`published_at`、`metadata`（feed URL / feed title）。

`select_candidates()` 以 0 次模型调用完成，顺序固定：

```text
canonical_url()  去 fragment、去 utm_*/fbclid/gclid/mc_cid/mc_eid、统一 host 大小写与 www.、去尾斜杠
→ 跨源 dedupe（同一 canonical URL 只保留一条，空 URL 直接丢弃）
→ garbage filtering（广告/招聘/newsletter 关键词、导航类精确标题、短标题 < 6 字符、/jobs/ 等 URL）
→ recency filtering（published_at 已知且超过 MAX_CANDIDATE_AGE_HOURS 的丢弃；日期未知的保留）
→ 组内按 published_at 倒序
→ 按来源 round-robin 取候选
→ MAX_CANDIDATES_PER_RUN（默认 6）截断
```

正文侧：`ArticleExtractor`（Jina Reader，3 次尝试、429/5xx 退避）→ `clean_extracted_text()`（去 reader 元数据头、图片行、分隔线、重复行；按 `ARTICLE_CONTENT_CHARS` 在段落边界截断）。

## Stage 2 — Researcher

- 输入：候选元数据 + cleaned 正文 + editorial scope + output schema。
- 输出：`decision`（KEEP/SKIP）、`reason`、`topic`、`entities`（companies/products/models/events）、`key_numbers`、`gaps`、1-4 条 `notes`（claim/evidence/why_it_matters/confidence）。
- `source` / `url` / `published_at` 由确定性代码注入，不由模型生成。
- `entities` / `key_numbers` / `gaps` 缺失时降级为空集合，不判定候选失败；KEEP 必须有至少一条 note。
- 是唯一读取正文的 AI 节点，正文只存在于该次调用的 payload 中。

## Stage 3 — Writer

- compose：`editorial_brief` + `research_notes` + 空 `revision_brief` + `output_schema`。
- revise：额外携带 `previous_draft`，仍使用同一份 `research_notes`，不携带正文。
- 不访问网页、不新增事实、不改变 Research Notes。
- 输出 Draft：`headline` / `summary` / `key_facts` / `why_it_matters` / `telegram_copy`。
- 网站条目直接使用 `headline`（标题）和 `summary`（一到三句摘要）；schema 与 instruction 都写明这两个字段会原样显示在网站上。`telegram_copy` 只在 `PUBLISH_TELEGRAM=1` 时使用。

## Stage 4 — Reviewer

- 输入：`research_notes` + `draft` + `rubric` + `output_schema`。
- 输出：`status`（PASS/REVISE/REJECT）、`scores`（factuality/relevance/clarity）、`issues`、`revision_brief`。
- 确定性约束：PASS 时 `revision_brief` 必须为空，且 factuality/relevance ≥ 8、无 major issue；REVISE 必须有 `revision_brief`；REJECT 必须有 `issues`。
- 语义拒绝与基础设施失败严格分离。

## Stage 5 — Revision Loop

`revision_count` 表示已使用的返工次数。默认 `MAX_REVISIONS=1`：

- 第一次 REVISE → 计数 +1 并回到 Writer（Writer 看到 `previous_draft` + `revision_brief`）。
- 再次 REVISE 且已达上限 → `HOLD`，候选以 `SKIPPED` 结束。
- 循环有硬上限；返工不重新调用 Researcher，事实基线不变。

## Stage 6 — Publisher 与 Website

`PublisherNode` 是图内的 PASS 守卫：只有图状态与 `review.status` 同时为 PASS 的条目才能进入 `PUBLISHED` 终态。

- 默认（`PUBLISH_TELEGRAM=0`）：节点不做网络调用，runner 收集 `PUBLISHED` 条目。
- `PUBLISH_TELEGRAM=1`：逐条调用 Telegram `publish(copy, source_url)`，Markdown 400 时回退纯文本；运维告警走 `publish_alert(message)`，不带原文链接，也**不会**调用 Gemini。
- Publisher 与 AI 节点完全解耦，不感知模型与 prompt。

全部候选处理完后，runner 调用 `website.SitePublisher.publish(items, outcome)`：

- 条目映射 `site_item(candidate, draft)`：`title = draft.headline`、`summary = draft.summary`（空白规整，超过三句按句末标点截断）、`source = {name: RSS 源名, url: 候选 URL}`；只接受 http(s) URL，聚合源（hnrss）用文章域名作源名。
- 存储：`GitHubSiteStore`（Git Data API：读 ref → 读 `stats.json` 与当天文件 → `POST git/trees`（base_tree + 两个文件）→ `POST git/commits` → `PATCH git/refs/heads/<branch>`，`force=false`）。ref 更新返回 409/422 时重新读取并重算，最多 3 次；429/5xx/网络错误每个请求最多重试 3 次。`LocalSiteStore` 用于 dry-run，写同样的文件到本地目录。
- 有条目才写 `data/news/YYYY-MM-DD.json`（Asia/Shanghai 日期，同日重跑按 URL 去重合并）；`stats.json` 每轮都写，包括没有内容通过审核、所有来源不可用、配额账本失败等 FAILED 运行。
- 网站写入失败：记一条 `stage=site`、`PUBLISH_ERROR` 失败，本轮强制 `FAILED`（exit 1）并告警。
- `PUBLISH_ENABLED=0` 时 Telegram 跳过网络调用，网站自动改为 `local`（dry-run）。

### stats.json

`total_runs` = `daily_news.yml` 的定时运行次数，`success_rate` = 其中结论为 success 的比例（4 位小数），`since` = `2026-01`，`updated_at` = 本次发布时间（+08:00）。`compute_stats` 同时计算：

- **actions-api**：`GET /repos/{repo}/actions/workflows/daily_news.yml/runs?event=schedule` 里全部已完成的运行（排除本轮）+ 本轮；
- **carried**：上一份 `stats.json` + 其 `updated_at` 之后创建的已完成定时运行 + 本轮；

取总数大的（相等用 API）。GitHub 自 2026-10-01 起对公开仓库的 workflow run 记录最多保留 90 天，API 总数会随清理变小，此后由 carried 接管，保证不倒退。上一份成功次数 = `round(success_rate × total_runs)`。本轮只在 `GITHUB_EVENT_NAME=schedule` 且上一份 stats 写于本轮开始之前时计入（re-run attempt 用运行的 `created_at` 判断），避免重复计数。读取运行历史使用工作流内置的只读 `GITHUB_TOKEN`（`actions: read`）。

## Graph State

```python
{
    "candidate": {...},          # Candidate
    "research_notes": {...},     # Research Notes
    "draft": {...},              # Draft
    "review": {...},             # Review
    "revision_brief": [],
    "revision_count": 0,
    "status": "NEW",             # 图内状态
    "published": False,
}
```

图内状态：`NEW → RESEARCHED → DRAFTED → PASS|REVISE|REJECT`；终态为 `SKIP` / `REJECT` / `HOLD` / `PUBLISHED`。正文只存在于 Researcher 调用的局部 payload。

## Data contracts

```json
// Candidate
{"id": "7f3a1c2b9d", "title": "...", "url": "https://...", "source": "...",
 "published_at": "2026-09-23T02:00:00+00:00",
 "metadata": {"feed_url": "https://...", "feed_title": "..."}}

// Research Notes
{"decision": "KEEP", "reason": "...", "topic": "...", "source": "...", "url": "...",
 "published_at": "...",
 "entities": {"companies": [], "products": [], "models": [], "events": []},
 "key_numbers": [{"label": "capacity increase", "value": "50%"}],
 "gaps": ["..."],
 "notes": [{"claim": "...", "evidence": "...", "why_it_matters": "...", "confidence": 0.9}]}

// Draft
{"headline": "...", "summary": "...", "key_facts": ["..."],
 "why_it_matters": "...", "telegram_copy": "..."}

// Review
{"status": "REVISE", "scores": {"factuality": 9, "relevance": 8, "clarity": 9},
 "issues": [{"severity": "major", "problem": "..."}], "revision_brief": ["..."]}
```

AI 输出先经过 `responseSchema` 约束，再经过本地 validator；缺字段、非法枚举或非 JSON 对象记为当前候选失败（`SCHEMA_INVALID`）。

## Gemini client

- 请求：`x-goog-api-key` header；`responseMimeType=application/json`；可选 `responseSchema`、`maxOutputTokens`、`thinkingConfig.thinkingLevel`。
- 超时与预算：`GEMINI_TIMEOUT_SECONDS=180`；单次逻辑调用 `GEMINI_CALL_BUDGET_SECONDS=240`；整场 `RUN_BUDGET_SECONDS=2100`；HTTP timeout clamp 到剩余预算。
- 重试：每个模型最多 `GEMINI_MAX_ATTEMPTS=3`；429 根据安全的 quota dimension 区分日配额，按 `Retry-After` / `RetryInfo` 等待，并共享至少 60s 模型冷却；明确日配额耗尽不短重试。超过 120s 等待上限/deadline 时结束本轮，不提前重试。5xx 退避 10/20/40s，network / timeout 退避 2/4/8s，有 jitter。
- 配额：每次 POST 都经过共享 `QuotaGuard`；默认 15s 模型间隔、18 次/模型/Pacific 配额日。发送前保存预留，失败不返还。账本损坏/读取失败禁止生成；未保存账本的运行保守预留剩余额度。每日重置跟随 `America/Los_Angeles` 夏令时。
- 模型 fallback：主模型 429 / 5xx 重试用尽后，切换到 `GEMINI_FALLBACK_MODELS` 中第一个与主模型不同的模型再完整尝试一轮；每次逻辑调用最多切换一次，共享同一 deadline；网络错误与 4xx 配置/计费错误不切换。breaker 只记录逻辑调用的最终结果，记录 `model_fallbacks`。
- 日志：`purpose / attempt / http / retry_after / backoff / reason`，不含 key、header、请求体。
- JSON 容错：直接解析 → fenced 提取 → 一次只修格式的 repair（与原调用共享 deadline，走完整 retry/超时路径）→ 仍失败抛 `GeminiResponseError`；截断抛 `GeminiTruncatedResponseError`。
- thinking 兼容：模型 400 明确拒绝 `thinkingConfig` 时本次运行降级为默认值并记录 `thinking_downgrades`。

## Failure semantics

```text
TRANSIENT_RATE_LIMIT / TRANSIENT_SERVER / TRANSIENT_NETWORK   计入 breaker
MODEL_RESPONSE_INVALID / MODEL_RESPONSE_TRUNCATED / SCHEMA_INVALID   候选级
SOURCE_ERROR / PUBLISH_ERROR    候选级
CONFIG_ERROR                    停止本轮（不重试、不换模型；含 401/403 与预付费耗尽 402）
UNEXPECTED_ERROR                停止本轮且 run 强制 FAILED
```

`errors.classify_failure(stage, cause)` 输出类别 + `transient` / `stop_run` / `force_failed`。

### Breaker

- 窗口 `BREAKER_WINDOW=5`，阈值 `BREAKER_THRESHOLD=3`。
- 粒度 = logical call：每个 Researcher/Writer/Reviewer 调用（含 revision）记录恰好一个事件。
- 瞬态失败计数；`SchemaError`/`GeminiResponseError` 占位但不计数；`SOURCE_ERROR` 不进入窗口。
- 窗口填满且瞬态失败达到阈值 → 终止本轮后续候选；已在窗口内被成功挤出则视为恢复。

### Run outcome

候选级只有 `PUBLISHED` / `SKIPPED` / `FAILED`。

| Outcome | 条件 | exit |
| --- | --- | --- |
| `SUCCESS` | 无候选失败，且未提前停止 | 0 |
| `PARTIAL_SUCCESS` | 有失败或提前停止（breaker / 时间预算），但 `published > 0` | 0 |
| `QUOTA_STOPPED` | 日配额/预算主动停止，且没有其他真实失败；剩余候选记入 `deferred` | 0 |
| `FAILED` | `published == 0` 且存在失败 / 提前停止；或程序级故障；或网站写入失败 | 1 |

全部候选被编辑规则跳过（`published == 0`、无失败）属于正常完成：pipeline 工作正常，只是没有合格内容。

## Configuration

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `GEMINI_API_KEY` | 必填 | Gemini 认证 |
| `RESEARCHER_MODEL` / `WRITER_MODEL` / `REVIEWER_MODEL` | 必填 | 每节点独立模型 |
| `*_THINKING_LEVEL` | `low` | 每节点 thinking |
| `RESEARCHER_MAX_OUTPUT_TOKENS` / `WRITER_...` / `REVIEWER_...` | 3072 / 2560 / 1024 | 每节点输出上限 |
| `SOURCE_FEEDS` | 内置 5 源 | 逗号或换行分隔 |
| `ARTICLES_PER_FEED` | `2` | 每源取多少条 |
| `MAX_CANDIDATES_PER_RUN` | `6` | 每轮候选上限 |
| `MAX_CANDIDATE_AGE_HOURS` | `72` | recency 过滤，`0` 关闭 |
| `MAX_REVISIONS` | `1` | 返工上限 |
| `ARTICLE_CONTENT_CHARS` | `12000` | 正文输入上限 |
| `GEMINI_TIMEOUT_SECONDS` / `GEMINI_MAX_ATTEMPTS` / `GEMINI_CALL_BUDGET_SECONDS` | 180 / 3 / 240 | retry 与超时 |
| `GEMINI_FALLBACK_MODELS` | 空（关闭） | 逗号分隔的备用模型，按顺序取第一个与主模型不同的 |
| `BREAKER_WINDOW` / `BREAKER_THRESHOLD` | 5 / 3 | breaker |
| `RUN_BUDGET_SECONDS` | `2100` | 整场时间预算 |
| `SITE_REPO_TOKEN` | github 模式必填 | 网站仓库 fine-grained token（Contents 读写，仅 MartinKuai/martinkuai） |
| `SITE_PUBLISH_MODE` | `github` | `github` / `local`（dry-run）/ `off` |
| `SITE_REPO` / `SITE_REPO_BRANCH` / `SITE_NEWS_DIR` | `MartinKuai/martinkuai` / `main` / `data/news` | 写入位置 |
| `SITE_LOCAL_DIR` | `output/site` | dry-run 输出目录 |
| `SITE_TIMEOUT_SECONDS` | `20` | GitHub 请求超时 |
| `SITE_STATS_SINCE` / `SITE_STATS_WORKFLOW` | `2026-01` / `daily_news.yml` | stats 口径 |
| `PUBLISH_TELEGRAM` | `0` | `1` = 恢复 Telegram 逐条推送与告警 |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | `PUBLISH_TELEGRAM=1` 时必填 | Telegram 发布与通知 |
| `PUBLISH_ENABLED` | `1` | `0` = 不对外发布（Telegram 跳过，网站改为 local） |
| `RUN_SUMMARY_PATH` | 空 | 设置后写出 JSON summary artifact |

## Runner 与 Observability

`runner.run_daily()` 按候选顺序执行，每个候选输出：

```text
Candidate start | id=... | source=... | title=...
Candidate step  | id=... | node=researcher | status=RESEARCHED
Candidate step  | id=... | node=writer     | status=DRAFTED
Candidate step  | id=... | node=reviewer   | status=REVISE
Candidate done  | id=... | status=PUBLISHED | reason=published | revisions=1
Candidate failed| id=... | node=writer | category=TRANSIENT_SERVER | error=GeminiAPIError
```

随后输出 `Run summary`（discovered / selected / processed / published / skipped 细分 / failed / deferred / revisions / breaker / models / outcome）、按模型请求数、HTTP 状态计数、各节点 calls / success / failures，以及 `Website:`（mode / items / news_file / total_runs / success_rate / basis / commit）。`FAILED`、`PARTIAL_SUCCESS` 与 `QUOTA_STOPPED` 发出纯文本告警，列出跳过/待处理数、失败阶段、HTTP 错误分布及配额停止原因：`PUBLISH_TELEGRAM=1` 时发到 Telegram，否则写入运行日志、Actions warning 注释与 Job Summary（FAILED 运行 exit 1，GitHub 另发失败邮件）。

## Secret boundary

- 本地凭证在 Git 忽略的 `.env`；CI 从 Repository Secrets 读取；模型名来自 Repository Variables。
- 网站写入只用 `SITE_REPO_TOKEN`（fine-grained，仅 Contents 读写、仅网站仓库）；工作流内置 token 保持 `contents: read` / `actions: read`。GitHub 请求失败只记录 HTTP 状态码，不记录 URL、header 或响应体。
- Gemini key 只经 `x-goog-api-key` 发送；summary、失败记录与 retry 日志只输出安全字段。
- 不保存 prompt 内容、正文或模型原始输出。
