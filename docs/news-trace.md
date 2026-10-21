# 每日 AI 新闻运行记录

每次 `run_daily()` 在 `github` 或 `local` 模式结束时，新增
`data/news/YYYY-MM-DD.trace.json`，供网站 `/work/daily-ai-news/` 回放。
trace 与本轮新闻（有条目时）和 `stats.json` 交给同一次存储提交；没有合格条目也写 trace。
同日重跑覆盖当天 trace，只保留最近一轮。原有日新闻同日按 URL 合并的行为、两个既有 JSON 的格式和统计口径均不变。

## 字段与真实流水线的对应

流水线按候选逐条运行：Researcher → Writer → Reviewer → 发布守卫。
Reviewer 逐条审查，`REVISE` 回到 Writer，达到上限后为 `HOLD`。没有整份简报的模型审查。
runner 消费 LangGraph 的 `stream_mode="updates"`，每个节点成功返回后立即收集其经过校验的输出；后续异常不会丢掉已完成阶段。

| 字段 | 来源和含义 |
| --- | --- |
| `date` | 本次网站写入的北京时间日期，与文件名和同次新闻写入日期一致 |
| `run.number` | 同次 `stats.json.total_runs`；仍只累计定时运行，手动运行不另加编号 |
| `run.started_at` | 发布器创建时记录的运行开始时间，`+08:00` |
| `run.finished_at` | 编辑流程结束、准备本次存储提交的时间，`+08:00`；不包含之后 GitHub 网络提交和通知的耗时 |
| `run.status` | 下表的固定枚举映射 |
| `candidates[].id` | 现有候选的 canonical URL 短哈希；脱敏后的 ID 最多 64 字符 |
| `title` | RSS 原文标题，保留原语言，最长 140 字 |
| `source.name` | 沿用现有网站来源规则，聚合源使用原文域名，最长 120 字 |
| `source.url` | 原文 HTTP(S) 绝对地址；非法地址、嵌入认证信息、敏感查询参数或已知敏感值时为 `null` |
| `researcher` | `KEEP/SKIP → keep/skip`，原因和最多 4 条 `notes[].claim`；不是完整研究对象 |
| `writer` | 最近一次成功生成的 `headline` 和 `summary`，摘要至多三句 |
| `reviewer` | 最近一次有效审查：`PASS/REVISE/REJECT → approve/revise/reject`；`HOLD` 仍是 `revise` |
| `reviewer.rounds` | 实际返回有效审查结果的次数，包含返工后审查；不累计 HTTP 重试、格式修复或失败调用 |
| `reviewer.issues` | 最后有效审查的问题及修改要求去重后最多 4 条；`approve` 一律为空数组 |
| `published` | 该条通过审查、能够转为网站条目并进入本次网站写入，布尔值；不是仅看图内 `PUBLISHED` |
| `counts` | 从最终导出的候选重新计算 fetched / kept / drafted / approved / published |

### 候选范围

`candidates` 是**确定性预筛选后选入本轮编辑流程的候选**，不是 RSS 的全部原始条目。
包括选中但因配额、预算或故障尚未处理的候选；未完成的阶段是 `null`。
这样不会把规则去重、过期过滤或额度不足伪装成 Researcher 的 `skip`。
RSS 原始数量仍在现有 run summary 的 `discovered` 中。
trace 最多保留 40 条，计数只描述这 40 条；配置校验允许的候选上限为 20，工作流实际为 6，无需改变处理上限。

网站当前会把 `researcher: null` 显示为跳过；数据仍保留 `null`，不制造 Researcher 决策。本任务没有改动网站代码。

### 运行状态映射

| 实际 outcome | trace `run.status` |
| --- | --- |
| `SUCCESS`，有网站发布条目且没有已通过但未写入的条目 | `success` |
| `SUCCESS`，没有条目通过审查 | `no_items` |
| `SUCCESS`，存在审查通过但无法转为网站条目的候选 | `partial_success` |
| `PARTIAL_SUCCESS` | `partial_success` |
| `QUOTA_STOPPED` | `quota_stopped` |
| `FAILED` | `partial_success` |

固定约定没有 `failed`，因此最后一行是有损映射：trace 的 `partial_success` 表示存在异常或未完整完成，**不保证发布数大于零**。
真实运维结果继续是 `FAILED`、退出码 1 和原有告警；不会把故障记成正常 `no_items`，也不改变 `stats.json` 的成功率计算。
当前网站会将这个枚举显示为“部分成功”，不能仅凭网站状态判断运行是否成功，应以 Actions 结果和运行摘要为准。

### 返工快照的限制

固定格式只容纳一份 Writer 和 Reviewer，保存的是各自最近一次有效结果，不是完整事件日志。
返工成功后展示最终稿、`approve`、总轮数、空问题数组；首稿和已解决的问题不导出。
若第二次审查失败，则保留修改后的草稿及上一次有效审查结论；`rounds` 不把失败调用算成一轮有效结果。

## 长度、大小和信息保护

- `title` / `reason` / 每条 `notes` / `headline` / `summary` / 每条 `issues` 上限分别为 140 / 80 / 120 / 80 / 240 / 100 字。省略号包含在上限里；按 Unicode 字符计数。
- `notes`、`issues` 分别最多 4 条。原因、要点和问题要求中文；模型若返回纯英文或空原因，使用“未提供中文……”的明确占位，不另调模型翻译或编造原因。
- 字段先去 HTML、脚本、样式和控制字符，只保留纯文本。仅复制允许展示的字段，不导出正文、metadata、评分、完整模型输出、HTTP 请求或异常对象。
- 对运行时已知的敏感值、常见认证格式和系统指令回显做删除处理；带凭证的来源 URL 置空。脱敏不改变实际模型输出或原有新闻 JSON。
- 最终使用 UTF-8 紧凑 JSON，总大小最多 **60,000 字节**（包含结尾换行，比 60 KiB 更保守）。若字段各自合法但总量过大，继续缩短文本，保留候选、决策、阶段、轮数和计数；仅在总量超限时将序列化后超过 512 UTF-8 字节的 URL 置空。继续收缩两轮仍超限时置空剩余 URL，不把截断地址冒充有效链接。

## 本地复现

安装仓库原有依赖后，在仓库根目录执行：

```bash
python scripts/export_trace_sample.py --output output/trace-sample
```

该命令实际运行 Researcher、Writer、Reviewer、LangGraph 路由和 `LocalSiteStore`。
模型响应与正文由预设的虚构样例提供，无网络请求，不读取 `.env` 或运行时凭证。
输出当天的新闻 JSON、trace 和统计 JSON，不写网站仓库。
独立本地目录没有累计统计且以手动模式运行，样例 `run.number` 为 0，符合“不累计手动运行”的既有规则。

样例包含五条：直接通过、返工后通过、研究跳过、审查拒绝、达到返工上限。
`counts` 为 `5 / 4 / 4 / 2 / 2`。一份实际生成的样例在 [examples/2026-10-21.trace.json](examples/2026-10-21.trace.json)。

```bash
python -m unittest discover -s tests -t tests
```

测试覆盖固定字段、合法枚举、计数、文本/数组上限、非法 URL、纯文本与信息保护、中文占位、大小上限、逐节点进度保留、全部未通过、配额停止、源故障、同日覆盖，以及 GitHub 同次提交与冲突重算。
现有 golden fixture 的新闻和统计输出保持逐字节一致，仅新增 trace fixture。

## 合并与运行边界

合并本分支后，现有 `github` / `local` 模式自动增加 trace，不需要增加 Secrets、变量、权限或依赖。
等待下一次既有定时运行即可；本任务没有手动触发 Actions，也没有向网站仓库写入。
本地验证替代了外部服务响应，不能证明实际在线模型或 GitHub 写权限已通过验证。

`SITE_PUBLISH_MODE=off` 明确关闭网站数据导出。程序尚未启动（安装依赖或前置步骤失败）、配置解析失败、进程被强制终止时无法生成 trace；网站写入通道本身故障也不能保证送达。
进入 runner 后被处理的来源、配额初始化或候选故障会尽量与统计一起写 trace；网站提交失败保留原有失败退出和告警，不伪造成功文件。
