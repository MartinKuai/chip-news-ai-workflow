# 每日 AI 新闻 trace 交付记录

日期：2026-10-21。开发分支：`codex/news-trace`。
基于 `main` 的 `16470b3600bcb6f1a171dbd515bb4c823edb1c08` 创建。

## 实现方式

- runner 消费 LangGraph 的逐节点输出，保存每条候选已完成的 Researcher、Writer、Reviewer 阶段；后续节点失败不会抹掉此前结果。
- `TraceRecorder` 只复制固定约定允许的字段，处理文本截断、纯文本、中文诊断占位、非法或敏感 URL、已知敏感值和指令回显，并限制最终 JSON 在 60,000 字节内。
- 网站发布器在同一次提交中加入 `YYYY-MM-DD.trace.json`。全部未通过或被研究员跳过时仍写 trace；同日重跑覆盖 trace，原有新闻与统计格式不变。
- 并发写入冲突时重新读取统计，并同步重新生成 trace 的 `run.number`，避免同次提交中的编号不一致。

实际流水线是**逐候选研究、写作和审查**，不是整份简报审查。
`KEEP/SKIP → keep/skip`，`PASS/REVISE/REJECT → approve/revise/reject`，返工达到上限的 `HOLD → revise`。
`rounds` 累计有效审查结果，不累计 HTTP 重试或失败调用。Writer 与 Reviewer 各保存最近一次成功输出；最终通过后 `issues=[]`。

需注意的有损映射：

1. `fetched` 是选入编辑流程的候选数，RSS 原始总数仍在运行摘要里。被选中但未执行的阶段写 `null`，不伪造 `skip`。
2. 固定状态没有 `failed`，实际 `FAILED` 在 trace 映射为 `partial_success`，可能发布 0 条。原退出码 1、告警及统计失败口径保留。网站显示的“部分成功”不足以判断实际运维结果。
3. 固定格式没有逐轮历史，不能同时保存首稿、每轮问题和最终稿。返工只通过最近快照与轮数表示。
4. 网站当前将 `researcher: null` 显示为跳过；导出数据仍保留真实 `null`。本任务没有修改网站。

完整字段和边界见 [news-trace.md](news-trace.md)。

## 验证结果

使用与 Actions 相同的 Python 3.11 系列（本地 3.11.9），安装仓库现有依赖；LangGraph 1.2.16。

```bash
python -m unittest discover -s tests -t tests
```

**301 项测试全部通过**，其中新增 `test_trace.py` 的 18 项测试。
覆盖固定字段与取值、计数一致、文本与数组截断、非法 URL 置空、纯文本与信息保护、中文诊断占位、总大小上限、逐节点异常保留、全部未通过、配额停止、前期来源/配额初始化失败、同日覆盖、GitHub 同次提交和并发冲突重算。

现有 golden fixture 的 `2026-10-20.json`、`stats.json` 逐字节未变，仅增加同日 trace fixture。
新增代码及 trace 测试的 Ruff 检查通过；改动原文件的检查排除了原有 `E501` 长行规则，其余通过。`git diff --check` 通过。

所有 GitHub 写入测试使用模拟 HTTP；本地样例使用预设响应。未调用在线模型，也未验证生产 GitHub 写入权限或线上回放渲染。

## local 模式实际输出

实际执行：

```bash
python scripts/export_trace_sample.py --output output/trace-sample
```

命令运行真实角色节点、真实 LangGraph 路由和 `LocalSiteStore`，用预设的虚构模型响应和正文替代外部调用，不读取 `.env`，不使用凭证，不访问网络。

生成的 [2026-10-21.trace.json](examples/2026-10-21.trace.json) 已原样复制到文档目录，**2,777 字节**，与 local 输出逐字节相同。
样例 `run.number=0`：空本地目录以手动模式运行，不增加仅统计定时运行的既有计数。

| 候选 | 研究结果 | 最后审查 | 有效审查轮数 | 网站写入 |
| --- | --- | --- | --- | --- |
| 示例一 | keep | approve | 1 | true |
| 示例二 | keep | approve | 2 | true |
| 示例三 | skip | null | 未审查 | false |
| 示例四 | keep | reject | 1 | false |
| 示例五 | keep | revise（HOLD） | 2 | false |

`counts = {fetched: 5, kept: 4, drafted: 4, approved: 2, published: 2}`。

## 合并后需要做什么

由本人决定何时将 `codex/news-trace` 合并到 `main`。现有 `github` / `local` 模式会自动导出 trace，无需新增 Secrets、变量、权限或依赖。
等待下一次既有定时运行后，检查网站目标分支同一次提交中包含 trace 和统计；有合格条目时也包含新闻 JSON，然后确认网站部署后的回放。

本任务只交付开发分支：没有合并 main、没有开 PR、没有手动触发工作流、没有修改仓库 Secrets/变量/设置，也没有向 `MartinKuai/martinkuai` 写入任何内容。

程序未启动、配置解析失败、进程被强制终止或网站写入通道故障时，无法保证网站收到 trace；进入 runner 后被处理的故障会尽量随统计一起写入，真实故障仍由原运维告警和 Actions 结果记录。
