# Daily Chip News 架构

## Runtime flow

一次日报运行包含来源收集、逐篇 AI Graph 和确定性发布三个阶段：

```text
RSS feeds
  ↓ collect + deduplicate
Article candidates
  ↓ one StateGraph invocation per article
Researcher
  ├─ SKIP ───────────────────────────────→ END
  ↓ KEEP
Writer
  ↓
Reviewer
  ├─ PASS ─────────→ Publisher ─────────→ END
  ├─ REJECT + revision_count < limit ───→ Writer
  └─ REJECT + revision_count >= limit ──→ HOLD → END
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
- 所有来源均不可用时抛出 `SourceCollectionError`，输出失败 summary 并以非零状态退出。

来源失败记录只包含来源 URL / 名称和异常类型。

## Node responsibility

### Researcher

Researcher 接收候选文章元数据，通过 Jina Reader 获取原始正文，依据 editorial scope 判断相关性并提取证据。它输出 Structured Research Notes；语义无关时返回 `SKIP`。

### Writer

Writer 每次调用都重新构造上下文：

```text
Editorial Brief
+ Structured Research Notes
+ Revision Brief（返工时）
+ Output Schema
```

原始网页正文、RSS 历史和 Researcher Prompt 不进入 Writer payload。返工时仍从原 Research Notes 开始，仅增加精简 `revision_brief`。

### Reviewer

Reviewer 接收 Research Notes、Draft、Rubric 和输出结构。Rubric 覆盖 factual grounding、source support、unsupported claims、commercial relevance、recency、clarity、duplication、tone、length 和 format。输出为机器可读的 `PASS` / `REJECT`、评分、问题和 Revision Brief。

### Publisher

Publisher 为确定性发布层。图状态与 `review.status` 同时为 `PASS` 时发送 Telegram。Markdown 请求返回 400 时尝试一次纯文本 fallback；最终发送失败抛出 `PublisherError`。

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

原始正文只存在于 Researcher 单次调用的局部 payload，不写入 Graph State。

## Data contracts

### Research Notes

```json
{
  "decision": "KEEP",
  "reason": "...",
  "topic": "...",
  "source": "...",
  "url": "...",
  "published_at": "...",
  "notes": [
    {
      "claim": "...",
      "evidence": "...",
      "why_it_matters": "...",
      "confidence": 0.9
    }
  ]
}
```

### Draft

```json
{
  "headline": "...",
  "summary": "...",
  "key_facts": ["..."],
  "why_it_matters": "...",
  "telegram_copy": "..."
}
```

### Review

```json
{
  "status": "REJECT",
  "scores": {"factuality": 9, "relevance": 8, "clarity": 9},
  "issues": [{"severity": "major", "problem": "..."}],
  "revision_brief": ["..."]
}
```

AI 输出经过本地 schema validator。缺字段、非法枚举、无效分数或非 JSON 对象会作为当前文章失败记录。

## Context boundaries

```text
Source metadata + raw article + editorial scope
                         ↓
                    Researcher
                         ↓ Research Notes only
Editorial Brief + Research Notes + optional Revision Brief
                         ↓
                       Writer
                         ↓ Draft
Research Notes + Draft + Rubric
                         ↓
                      Reviewer
```

Reviewer 的完整输出不直接进入下一次 Writer 调用；仅 `revision_brief` 参与返工。

## Review routing

`revision_count` 表示已使用的返工次数。第一次 REJECT 后计数为 1 并返回 Writer；第二次 REJECT 后计数为 2 并再次返回 Writer；再一次 REJECT 则进入 `HOLD`。`MAX_REVISIONS=0` 时，初稿被拒后直接 HOLD。

终止状态：

- `SKIP`：内容不在编辑范围内，不发布。
- `PASS`：审核通过且 Publisher 完成发送。
- `HOLD`：达到返工上限，不发布。
- `FAILED`：当前文章处理失败，记录后继续下一篇。

## Failure semantics

### Source level

单个 RSS 解析失败只增加 `sources_failed`。至少一个来源可用时，`workflow_status` 保持 `PASS`；所有来源均失败时转为 run-level `SourceCollectionError`。

### Article level

图节点异常包装为带安全阶段信息的 `NodeExecutionError`：`researcher`、`writer`、`reviewer` 或 `publisher`。正文提取、JSON 解析、schema 校验和单篇瞬态请求失败计入 `failed`，后续文章继续运行；Writer 或 Reviewer 阶段的瞬态 Gemini 失败不会累计服务级不可用计数。

### Run level

以下情况使整次 workflow 失败：

- 所有 RSS 来源均不可用。
- Gemini 认证、权限或模型配置错误。
- 连续两篇文章在 Researcher 首次 Gemini 调用阶段、有限重试后仍发生 Gemini 网络、429 或 5xx 错误。
- Telegram 认证或目标配置错误。
- 未分类的程序运行错误。

全局中止前仍输出当前 Run summary：

```text
sources_total / sources_ok / sources_failed
candidates / processed / published / skipped / held / failed / revisions
workflow_status
```

## Model routing

三个节点分别绑定自己的配置项：

```text
Researcher ← RESEARCHER_MODEL
Writer     ← WRITER_MODEL
Reviewer   ← REVIEWER_MODEL
```

Gemini client 每次调用都显式接收 `model`、role instruction 和 payload。

## Secret boundary

- 本地凭证存放在 Git 忽略的 `.env` 或系统环境变量中。
- GitHub Actions 从 Repository Secrets 读取 Gemini 和 Telegram 凭证。
- 模型名称从 Repository Variables 读取。
- Gemini key 通过 `x-goog-api-key` header 发送。
- summary 和失败记录只输出安全字段与异常类型。
