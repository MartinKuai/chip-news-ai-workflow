# 模型服务可切换与免费通道验证

更新：2026-10-21。本轮继续使用 `codex/news-trace`，不合并 main、不创建 PR、不改变线上配置。

## 当前结论

三个角色的模型 ID 原本已经从环境变量读取，原先绑定的是 Gemini 的 HTTP 协议、错误类型和配额账本。
本轮抽出通用 `JSONClient` 与错误类型，并增加 Chat Completions 适配器。节点业务、发布质量闸门和网站数据格式不变。

**可切换的开发接入层已完成；OpenCode 免费 DS/GLM 通道尚未验证成功，当前实测为 403，不能作为已可用替代品交付。**

## 配置

默认 `LLM_PROVIDER=gemini` 保留现有行为；不会因合并代码而切换生产服务商。
使用其他兼容 API 时选择 `LLM_PROVIDER=openai-compatible`，见 [通用配置示例](../config/compatible.env.example)。

| 配置 | 含义 |
| --- | --- |
| `LLM_PROVIDER` | `gemini` 或 `openai-compatible` |
| `LLM_BASE_URL` | 服务商 HTTPS 基地址，不含凭证、查询参数或 `/chat/completions` 后缀 |
| `LLM_AUTH_MODE` | `bearer`（默认）或显式 `none`；none 不发送账户凭证，不加载 .netrc 或环境代理凭证 |
| `LLM_API_KEY` | bearer 模式必填；none 模式不使用。日志、配置 repr 和 trace 不输出其值 |
| `RESEARCHER_MODEL` / `WRITER_MODEL` / `REVIEWER_MODEL` | 三个角色各自的模型 ID，无内置默认型号 |
| `LLM_ALLOWED_MODELS` | 必填的明确允许名单，逗号分隔；所有主模型及备用模型必须在其中 |
| `LLM_FALLBACK_MODELS` | 显式备用名单；每次逻辑调用最多切换至其中第一个不同型号，不自动探索其他型号或服务商 |
| `LLM_RESPONSE_FORMAT` | `json_object` 或 `none`。不支持 JSON mode 的服务需明确选择 none；无论何种模式，角色输出仍须通过本地 schema 校验 |
| `LLM_TIMEOUT_SECONDS` | 单次 HTTP 超时，默认 120 秒 |
| `LLM_MAX_ATTEMPTS` | 每个型号的 HTTP 尝试数，默认 2 |
| `LLM_CALL_BUDGET_SECONDS` | 一次逻辑调用总预算，默认 240 秒；重试、备用及一次格式修复共享预算 |
| `LLM_MIN_INTERVAL_SECONDS` | 共享请求间隔，默认 2 秒；429 的等待不因切换模型而绕过 |

兼容模式不要求 `GEMINI_API_KEY`，也不读取 Gemini 的 Pacific-day 配额账本。
Actions 的账本恢复/上传步骤只在 Gemini 模式执行；新增变量仅接入工作流文件，**没有修改任何 Repository Secrets 或 Variables**。
兼容模式采用服务商自身配额管理，仍有请求间隔、尝试上限、总运行预算和原有服务熔断器。

400/401/402/403/404、重定向和认证配置错误立即停止，不用备用模型掩盖；只有 429/5xx 可尝试明确配置的备用型号。
不把 Gemini 的 thinking 字段发送到其他服务；只读取最终 `message.content`，不导出 reasoning 内容。
现有 run-summary 中 `gemini_requests` 等旧计数键保留兼容性，在兼容模式表示该模式的调用次数；日志新增 provider 标识。

允许名单保证不擅自切换到未配置的付费型号；**它不是服务商价格检测器**，免费活动是否结束仍须核验。
若使用免鉴权模式，付费模型按服务商要求应拒绝请求，而不是使用账户余额。

## OpenCode：文档与实际调用的证据

2026-10-21 读取了以下官方资料：

- [模型目录与免费活动](https://opencode.ai/v2/docs/console/models/)：列出 `deepseek-v4.1-flash-free`、`glm-5.3-flash-free`，两者均为限时免费。
- [Inference API](https://opencode.ai/v2/docs/console/api/inference/)：列出 `https://opencode.ai/inference/openai/v1/chat/completions`，并说明免费聊天模型可不带账户鉴权。
- [CLI](https://opencode.ai/docs/cli/)：支持 `opencode run` 非交互调用、JSON 事件输出与隔离插件。
- [Go](https://opencode.ai/v2/docs/console/go/)：Go/Go Plus 是订阅服务，定位于典型编码 Agent 流量，不应与免费目录混为一谈。本任务没有使用 Go。

实际测试结果：

| 检查 | 方法 | 结果 |
| --- | --- | --- |
| 免费型号是否存在 | `opencode models opencode` | 可列出 DS 与 GLM 两个免费 ID；仅证明目录存在 |
| 全新目录的型号可见性 | 隔离 XDG/HOME、禁用插件后刷新 models 缓存 | 两个免费 ID 均可列出；刷新前使用内置旧目录会报 ModelNotFound |
| 直接 HTTP，DS | 不提供任何账户凭证，最小文本请求 | HTTP 403，`FreeTierError` |
| 直接 HTTP，GLM | 同上 | HTTP 403，`FreeTierError` |
| 官方 CLI 1.18.33，DS | 隔离配置/缓存/数据目录，刷新目录，`run --pure --format json --agent plan` | 同样 HTTP 403，`FreeTierError` |
| 官方 CLI 1.18.33，GLM | 同上 | 同样 HTTP 403，`FreeTierError` |
| 新闻流程 local 试跑 | 实际模型 HTTP + 固定公开素材，提取和 RSS 不联网 | Researcher 收到 403，真实 outcome=FAILED；仍生成失败 trace 与统计，未生成新闻 |

服务端错误的含义是“免费层只能在 OpenCode 内使用”。隔离 CLI 也被拒绝，原因尚未证明；不能仅凭文档或模型列表宣称能运行。
没有伪造客户端身份或请求头，生成测试使用隔离配置与数据目录，没有导出或转用现有 OpenCode 凭证，也没有重试已被明确拒绝的型号到获得成功为止。
没有证据证明用户已登录的桌面客户端一定失败，也没有证据证明 GitHub Actions 上能成功；这些不能从本次结果推断。

开发试跑可复现：

```bash
python scripts/probe_free_models.py
```

脚本只读取 [试验配置](../config/opencode-free.env.example)，不读取 `.env` 或 OpenCode 凭证。
强制免鉴权、local 发布、关闭 Telegram、限制候选/重试/时间预算；输入是人工核实过的官方文档固定摘述，不伪造发布时间或性能数据。
**当前预期会失败返回退出码 1**，此配置不是生产默认值。
生成的 [失败 trace](examples/opencode-free-2026-10-21.trace.json) 保留真实未执行阶段；因固定 trace 枚举没有 failed，其 `partial_success` 是此前已说明的有损映射，真实 run outcome 仍为 FAILED。

## 验证与后续选择

Python 3.11 下全量 **318 项测试通过**，本轮新增 **17 项**。新增测试覆盖协议序列化、免鉴权不发送凭证、允许名单、HTTP 重试/等待/预算、禁止静默付费备用、格式修复、输出截断、通用错误分类及真实图路由的模拟 HTTP 测试。
模拟 HTTP 的完整图可以产生合格新闻和 trace，不能替代上述真实服务接入验证，更不能证明免费服务长期稳定。

适合继续验证的顺序：

1. 若用户现有 OpenCode 界面确实能使用免费 DS/GLM，先确认实际提供方、客户端版本和调用结果，再评估原生 CLI 是否适合在 CI 使用；不搬运登录状态、不伪装流量。
2. 或提供一个已确认可用、允许日更自动化的兼容 API，配置地址、凭证（如需要）及允许的模型，即可利用本轮适配层做 local 小样验收。
3. 至少验证 Researcher 的筛选、Writer 的 JSON 与事实约束、Reviewer 的放行/返工，以及重复运行的耗时和错误率后，才切换生产。

另一个可调查的候选是智谱官方 [GLM-4.7-Flash API](https://docs.bigmodel.cn/cn/guide/models/free/glm-4.7-flash)：文档提供 API Key 调用示例和结构化输出能力，不依赖 OpenCode 客户端。旧的 GLM-4.5-Flash 文档已经提示下线及路由迁移，不应继续照抄旧型号；本轮尚未确认新型号的实际零计费条件、账户限额或调用表现，因此不将其记作已验证的免费替代。

本轮没有合并 main、没有 PR、没有手动触发工作流、没有改仓库设置，也没有向网站仓库写入。
