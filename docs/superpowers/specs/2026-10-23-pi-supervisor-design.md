# pi 主管派工与原会话回传设计

日期：2026-10-23。

## 目标与现状

用户要求核对分支，并支持 pi 作为主管派工。验收行为是：在 pi 原交互会话派单，台账和裁决识别为 pi，执行者完成后结果回到同一主管会话，主管再读状态、过闸门和复核证据。

接手时主工作区在 `feature/codex-supervisor`，HEAD 与 main、最新 gitlab/main 都是 `07e5161`；Codex 自动回传与角色环境相关修改尚未提交。隔离分支 `feature/pi-supervisor` 的 `9a8b8c7` 仅保存这些既有内容，主工作区保持原样。

## 方案

采用现有本地等待器加 pi 原生扩展。Python 继续负责派工、轮次与结果状态，pi 扩展通过本机文件接收事件，并调用 `pi.sendMessage(..., { triggerTurn: true, deliverAs: "followUp" })` 触发原会话。等待不调用模型；结果处理正常消耗主管 Token。

现有手动 wait 可以作为扩展未加载时的后备；另起 pi 进程无法继续原交互主管，因此不采用。这里的实现选择由主管根据现有代码与 pi 1.4 API 作出。

## 约束与边界

- Python 3.9+，标准库；不增加 Python 或 Node 依赖。
- `AGENT_DISPATCH_WORKER` 和显式 `AGENT_DISPATCH_SESSION` 保持最高优先级；真实 pi 的 `PI_SESSION_ID` 在继承的 Claude/Codex 身份之前识别，pi 使用 `kind: "pi"`。
- 每张单首次派出时绑定主管 session ID，续派不得因当前操作者改变而换目标。
- Codex 的 `codex_notify` 记录、命令与行为向后兼容；pi 使用独立的 `pi_notify` 记录，状态输出应可诊断。
- 复用既有等待器、轮次锁与投递前状态检查；人工持有、叫停、已裁决、过时回合不触发自动处理。
- pi 本地通知按 session ID 隔离；扩展只处理当前会话，忙时 followUp，闲时 triggerTurn；去重、重载/恢复与 session 切换均要验证。
- 扩展由 `session_start` 启动，`session_shutdown` 清理；执行者与非交互单轮模式不启动长期监听。
- `AGENT_DISPATCH_PI_NOTIFY=0` 可以关闭；未装扩展时清楚提示手动 wait，不能谎报已被主管消费。
- 本次不改认证、模型池、数据库、用户全局 pi 配置，不连接业务环境，不 push。
- 所有测试使用临时派工台账并移除真实主管身份与通知路径，不能向本会话或 Codex 真会话发送测试消息。

## 文件职责

- `agent_dispatch/store.py`：pi 主管身份。
- `agent_dispatch/notify.py`：共用等待/抑制逻辑及 pi 本地通知传输；如需要隔离传输可新增 `agent_dispatch/pi_notify.py`，不复制整套等待器。
- `agent_dispatch/cli.py`：pi 通知展示、重试与扩展所需的最小内部接口。
- `.pi/extensions/agent-dispatch-supervisor.ts`：会话内通知接收器；可显式以 `pi -e <path>` 加载。
- `tests/test_dispatch.py` 与聚焦的新测试：身份、完整派工回传、隔离、轮次及扩展生命周期。
- README.md、SUPERVISOR.md、SKILL.md：pi 启动/重载方法、自动回传与手动后备、可诊断限制。

## 验证

先跑现有身份及 Codex 专项作基线；按 TDD 添加 pi 场景，验证 headless 与 tmux、reply、失败/中断、去重、关停、跨会话隔离。用可控的 pi ExtensionAPI harness 执行真实扩展代码，验证消息入队与生命周期；如可行，使用独立临时 pi 会话验证扩展实际加载。最终执行 `python3 -m unittest discover -s tests`，保存原始输出与四项计数，并由独立复核者检查新增差异。
