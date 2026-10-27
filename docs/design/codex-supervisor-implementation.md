# Codex 交互主管自动回传：实现与验证

日期：2026-10-23；分支：`feature/codex-supervisor`。代码已实现，未提交或推送。

## 最终实现

- store.identify_dispatcher 识别并验证 CODEX_THREAD_ID，兼容 CODEX_SESSION_ID；保留执行者、显式主管和 Claude Code 的既有优先级。
- start / reply / follow 成功后，属于 Codex 的作业自动登记每回合通知，并启动独立本地等待进程。headless 和 tmux 使用同一套回传。
- 等待进程仅检查台账，不调用模型。结束后执行 argv 形式的 codex queue，送回原 thread，不创建第二个主管。
- 通知使用独立回合文件锁去重，不长期持有作业锁。投递前检查回合与状态，人工持有、叫停、已裁决、过时结果被抑制。
- 每回合的目标、状态、次数、日志和 queue 回执记录在 codex_notify。入队不等于主管消费，也不等于验收通过。
- 明确失败可 notify-retry，最多 3 次；超时/中断后可能已入队的情况不自动重发。通知失败不覆盖执行者的结果。
- 更新仓库 README、SUPERVISOR.md、SKILL.md，并备份后同步本机已安装 Codex 技能，避免新会话继续使用旧的模型轮询 wait 用法。Claude 的等待流程不变。

这是独立本地通知进程，不是常驻系统服务。机器重启或进程被杀后，需检查已结束作业并显式补投；本版不做跨机器/任意远程端点自动发现。用户在通知入队前后改变作业状态仍存在跨进程竞态，因此消费时必须重新读取最新状态。

## 自动测试

命令：`python3 -m unittest discover -s tests -p test_dispatch.py -k CodexNotifyTest`。

专项 11 项全部通过，覆盖身份与原有优先级、headless 投递、快速完成、非 Codex 与关闭开关、reply 新回合、失败重试、超时不重发、并发去重、人工持有/叫停/过时状态，以及 tmux 回合完成。测试使用假 Codex 和独立临时台账，不向真实会话投递。

命令：`python3 -m unittest discover -s tests`。

全量 121 项，120 项通过、0 断言失败、1 错误、0 跳过，用时 367.869 秒。错误为既有 `DispatchTest.test_web_edits_models_and_pools` 的 GET `/api/models` 请求超过 5 秒；该接口同步调用本机模型枚举命令。没有修改超时或判据。

随后单独复测同一用例，1 项通过，用时 2.450 秒。全量单次记录仍如实保留为 errors=1，不改写为全量一次通过。

原始全量日志：`/private/tmp/ad-codex-implementation-ich8ulh0/unittest.log`，结果元数据同目录 unittest-result.json。

## 修改后真实派工

批次：`codex-auto-verify-20261023-062408`。

| 作业 | 执行方式 | 真实执行与证据 | 自动投递 |
|---|---|---|---|
| codex-auto-headless-20261023-062408 | pi headless | PASS，证据核对、闸门通过、已验收 | 仅 1 次 queue，目标原主管 UUID |
| codex-auto-tmux-20261023-062408 | pi tmux | PASS，证据核对、闸门通过、已验收并释放会话 | 仅 1 次 queue，目标原主管 UUID |

两张单均由正常 start 命令触发自动通知；没有使用临时 wait-and-notify 脚本，没有人工 queue。

主管 UUID：`01a16725-2750-7861-8beb-0c275961492f`。

- headless 回执消息：`01a1676f-804d-7083-9890-5bc6bb6e4fc3`。
- tmux 回执消息：`01a1676f-76a9-70a2-b7e7-8e56229907ee`。
- 工作与证据：`/private/tmp/ad-codex-real-d31n9p8q/`。
- 截至本记录初次写入，两条消息已自动入队，待本轮回复结束后确认原会话自动消费。此前 P0 已单独验证 queue 到原会话触发下一轮，但不能代替这两条新通知的消费证据。
- 后续确认：tmux 测试单的通知已自动进入原会话并触发处理；再次核对 thread UUID 一致、仅投递 1 次、作业此前已验收，因此只记录消费，不重复裁决。证据已写入 final-verification.json。
- 最终确认：headless 测试单的通知也已自动进入原会话并触发处理；核对同一 thread、仅投递 1 次、作业已验收。两种方式的“真实 pi 执行 → 本地等待 → 自动 queue → 原交互会话消费”闭环均 PASS；未重复派工或裁决。

## 工作区保护

原有 README.md、cli.py、definitions.py、test_dispatch.py 的修改已保留；definitions.py 与接手前副本完全一致，其余文件仅叠加本次适配。原始副本与 diff 位于 `/private/tmp/ad-codex-implementation-ich8ulh0/baseline/` 和 baseline.patch。

前置实施作业 codex-supervisor-impl-20261023-r1 已按用户指示停止，后续代码由当前 Codex 直接完成，不能把该停止作业判为实施 PASS。

## 后续测试隔离修复

真实临时通知 `codex-c1-t1` 进入主管会话后，定位到 ModelLimitTest.start_async 的独立环境构造漏清 CODEX_THREAD_ID / CODEX_SESSION_ID。它创建 c0—c5 并发测试作业，继承真实主管身份并触发通知；普通 cli 测试入口此前已清理，但不能覆盖该独立入口。

新增回归 `test_async_start_does_not_inherit_codex_supervisor`，在通知关闭的安全条件下模拟父进程带 Codex 身份，修复前准确失败（Codex == Codex），修复后通过。现将 cli 与 start_async 统一使用 child_env，测试进程默认关闭回传并移除真实 Codex 身份；测试 PATH 放置拒绝外发的假 codex，专项另行显式启用假传输。同时用假 pi/opencode 隔离网页用例的模型枚举，避免依赖宿主工具响应。

隔离回归 1 项通过（2.404 秒），通知专项 11 项重新通过（21.728 秒）。修复后全量 **122 项全部通过**，0 失败、0 错误、0 跳过，用时 367.909 秒。原始日志为 `/private/tmp/ad-codex-implementation-ich8ulh0/unittest-isolated.log`，元数据为 unittest-isolated-result.json。

旧测试已入队的临时消息不代表正式作业，不进行验收或续派。本次只读队列检查未得到可用响应，没有修改其他队列消息，也没有通过直接修改 Codex 存储来清队列。
