# Daily Chip News V2 架构

## Graph

V2 是一个最小 LangGraph `StateGraph`，不是自建 Graph 框架：

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
- 异常：基础设施、配置或 schema 失败；任务失败，不发布。

## Failure semantics

429 与 5xx 进行有限重试；Gemini 4xx 认证、权限和模型错误直接失败；网络错误在有限次数后失败；JSON 格式错误使用独立异常。任何这类错误都不会被转换成 `SKIP`。Telegram 最终失败也向上抛出，使 GitHub Actions 明确失败。

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

V2 不引入数据库、Redis、向量数据库、RAG、MCP、Celery、Docker 编排、多 Agent Supervisor 或不必要的持久化框架。图只保留内容自动化 PoC 和 QA 所需的最小状态与路由。
