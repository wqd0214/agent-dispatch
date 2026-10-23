# Codex 当前会话派工等待验证记录

日期：2026-10-23。分支：`feature/codex-supervisor`。

## 范围

用户授权创建真实测试派工单；主管保持当前交互会话，派 pi headless 在临时目录执行。未新增后台 Codex 主管，未修改业务工程或配置。这里只验证一个 headless 作业，尚未验证 tmux、断线重启和完整自动续派。

## 真实派工与执行结果

- 作业/批次：`codex-wait-probe-20261023-054014`。
- 角色：全局 `backend-dev`，模型由现有角色模型池选中，未手动指定模型。
- 后端：pi；模型：`cc-switch-zhipu-glm-copy/glm-5.3-flash`；方式：headless；无浏览器通道。
- 临时工作目录：`/private/tmp/codex-dispatch-probe-ys248hn8`。
- 作业第 1 轮正常结束，约 1 分 31 秒，`STATUS=PASS`。
- 独立读取 evidence.json 和 report.md，确认执行者实际等待 20.001 秒。
- 机械闸门通过：进程退出码 0，结果含 STATUS。
- 已验收 pi 测试任务，验收备注明确不代表 Codex 自动回传适配通过。

## 原生后台等待观察

使用 Codex 当前命令工具运行本地包装脚本；脚本调用一次 `agent-dispatch wait <作业> --timeout 300`，未调用 queue 或注入通知。命令工具提前返回运行中 session ID；主管没有反复执行 agent-dispatch wait。

- 等待开始：2026-10-23 05:40:54.439 UTC。
- 等待结束：2026-10-23 05:42:08.693 UTC。
- 本地等待用时：74.252 秒，退出码 0，输出作业待审查与 STATUS=PASS。
- 当前模型未收到独立的自动完成通知；约 05:42:39 UTC 由主管主动收集工具输出后确认结束。
- 主管观察时使用过两次工具层定时等待和证据检查，因此这不是“整个测试期间零模型调用”的证明。
- 当前轮次始终处于 active 状态；没有用该阶段证明“本轮结束后处于空闲状态时原生机制也绝对不能通知”。

判定：本地等待通过；当前工具调用方式下原会话自动继续未获证明。不能将命令在后台运行等同于自动唤醒主管。

## 显式结果回传候选

只读检查确认本机现有共享服务运行中，CLI/服务端均为 0.163.0，没有启动或重启服务。向当前 thread 调用一次 codex queue，发送同一作业的结果通知。

- 目标 thread：`01a16725-2750-7861-8beb-0c275961492f`，来自当前命令环境的 CODEX_THREAD_ID。
- 入队消息 ID：`01a16749-9130-71c2-ae32-88b77ec9e67b`。
- queue 退出码：0，返回 `Queued message ... for thread ...`。
- 后续确认：上一轮回复结束后，该入队消息作为输入自动进入原会话，并触发本轮处理。再次读取 CODEX_THREAD_ID，与 queue 目标 UUID 完全一致；结果写入 supervisor-verification.json 的 queue_receipt.model_consumed / consumed_at。

本次已区分并分别确认消息入队和原会话消费。未使用 codex exec/resume 创建或并发接管主管。测试中的 queue 由主管在作业结束后显式调用；“wait 结束后自动 queue”的无人干预串联尚未实现，因此不宣称正式自动适配已完成。

## 适配线索

当前进程已有 CODEX_THREAD_ID / CODEX_SESSION_ID，二者指向同一会话；现有 store.identify_dispatcher 不识别这两个变量，测试单被识别为“终端”。这提供了明确的身份适配点。

等待逻辑无需重做；原生完成通知尚未获证明，而 queue 向原会话回传已实测可用。可以优先实施“现有 wait + 作业完成时一次 queue 回传”的轻量脚本，补齐绑定、失败和去重，再验证完整无人干预串联。

## 证据

临时目录内：task.md、probe-meta.json、evidence.json、report.md、wait-start.json、wait-result.json、supervisor-verification.json。

原始作业台账：`~/.agent-dispatch/jobs/codex-wait-probe-20261023-054014/`，包含本轮派工、日志、结果、gate 与验收记录。

当前仅为测试与证据记录，没有修改 agent-dispatch 的运行代码。结论：pi headless 执行 PASS；本地 wait PASS；queue → 原交互会话自动继续 PASS。原生自动通知未获证明，tmux 和中断恢复尚未测试，正式无人干预闭环尚未实现。
