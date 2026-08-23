# Daily Chip News 架构

## Graph

项目使用最小 LangGraph `StateGraph`，不是自建 Graph 框架：

```text
START
  ↓
Researcher
  ├─ SKIP ───────────────────────────────→ END
  ↓ KEEP
Writer
  ↓
Reviewer
  ├─ PASS ─────────→ Publisher ─────────→ END
  ├─ REJECT + revision_count < 2 ───────→ Writer
  └─ REJECT + revision_count >= 2 ──────→ HOLD → END
```

Researcher、Writer、Reviewer 是全部 AI Agent 节点。Publisher 只是确定性 StateGraph 节点，用来强制执行“Reviewer PASS 才可发送”的基础设施边界。

## Node responsibility

### Researcher

输入候选文章元数据，通过 Jina Reader 获取原始正文，依据 editorial scope 判断相关性并提取证据。它只输出 Research Notes；语义无关才返回 `SKIP`。抓取、网络、认证、quota、模型和 JSON 错误都抛出异常。

### Writer

每次调用重新构造 allow-listed fresh context：

```text
Editorial Brief
+ Structured Research Notes
+ Revision Brief（返工时）
+ Output Schema
```

它不接收原始网页正文、RSS 历史、Researcher Prompt、Researcher 完整上下文、Reviewer 长输出或隐藏推理。返工仍从原 Research Notes 开始，只增加精简 `revision_brief`。

### Reviewer

只接收 Research Notes、Draft、Rubric 和输出结构。它检查 factual grounding、source support、unsupported claims、commercial relevance、recency、clarity、duplication、tone、length、format，输出机器可读的 `PASS` / `REJECT`，不生成替代稿。

### Publisher

不调用 LLM。只有图状态与 `review.status` 同时为 `PASS` 才发送 Telegram。Markdown 400 会尝试一次纯文本 fallback；最终失败抛出异常。

## Graph State

单篇文章只维护最小状态：

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

原始正文只存在于 Researcher 单次调用的局部 payload，不写入 Graph State，因此不会沿图泄漏给 Writer。

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

所有 AI 输出先经过本地 schema validator；缺字段、错误枚举、非法分数或非 JSON 对象都会显式失败。

## Review routing

`revision_count` 表示已经获得的返工机会：初稿为 0。第一次 REJECT 后变为 1 并回到 Writer；第二次 REJECT 后变为 2 并再次回到 Writer；返工两次后的第三次 REJECT 进入 `HOLD`。`MAX_REVISIONS=0` 时初稿一旦被拒即 HOLD。

图的终止状态语义：

- `SKIP`：Researcher 的业务相关性判断；不发布。
- `PASS`：Reviewer 通过且 Publisher 成功；已发布。
- `HOLD`：达到返工上限仍被拒；不发布。
- `FAILED`：单篇抓取、解析、schema 或瞬态调用失败；记录后继续下一篇。
- 全局异常：配置、认证、模型、quota 或持续服务故障；输出 summary 后使任务失败。

## Failure semantics

每个图节点的异常都会包装为带安全阶段信息的 `NodeExecutionError`：`researcher`、`writer`、`reviewer` 或 `publisher`。批处理 runner 只记录文章标题、阶段和异常类型，不记录异常正文、Prompt、模型响应或凭证。

错误分为两级：

- **单篇错误**：正文抓取失败、Gemini JSON 解析失败、schema 校验失败、单篇 Telegram 瞬态失败，以及有限重试后的单次 Gemini 网络/5xx 故障。该文章计入 `failed`，后续文章继续。
- **全局错误**：Gemini 非 429 的 4xx 认证、权限或模型错误；Telegram 认证/目标配置错误；未知编程错误；或连续两篇文章在有限重试后仍发生 Gemini 网络/429/5xx 故障。runner 先输出当前 Run summary，再使 workflow 失败。

一次成功完成的图、一次有效的 Gemini 结构化响应，或已经到达 Publisher 的文章都会重置 Gemini 瞬态故障计数。这样单点失败不会停止日报，同时持续服务中断不会被误报为整批成功。任何基础设施错误仍不会转换成 `SKIP`。

统一 summary 包含 `candidates`、`processed`、`published`、`skipped`、`held`、`failed`、`revisions` 和 `workflow_status`。全局中止时 `processed` 可能小于 `candidates`。

## Secret boundary

- 本地真实值只在被 Git 忽略的 `.env` 或系统环境变量中。
- GitHub 使用 `secrets.GEMINI_API_KEY`，模型使用 Repository Variables。
- Gemini key 只通过 `x-goog-api-key` header 发送，不进入 query string。
- 日志不打印 API key、Telegram token、带凭证 URL 或完整环境变量。
- `.env.example` 只含安全占位值。

## Model routing

配置层要求三个独立变量，节点构造时各自绑定：

```text
Researcher ← RESEARCHER_MODEL
Writer     ← WRITER_MODEL
Reviewer   ← REVIEWER_MODEL
```

Gemini client 没有全局模型；每个 `request_json` 调用都显式传入 `model`、role instruction 和 payload。具体型号在创建新 API key 后按实际可用性配置。

## Deliberate exclusions

项目不引入数据库、Redis、向量数据库、RAG、MCP、Celery、Docker 编排、多 Agent Supervisor 或不必要的持久化框架。图只保留内容自动化 PoC 和 QA 所需的最小状态与路由。
