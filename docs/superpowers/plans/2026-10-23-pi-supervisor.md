# pi Supervisor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** pi 原交互会话能派工、保留主管身份，并自动收到执行结果继续复核。

**Architecture:** 在现有 Codex 本地等待器上增加 pi 文件通知传输，保留所有 Codex 行为与台账。pi 原生扩展接收当前 session 的事件，通过 followUp/triggerTurn 继续同一会话；监听和等待不调用模型。

**Tech Stack:** Python 3.9+ 标准库、pi 1.4 ExtensionAPI、TypeScript、Node.js 22。

**Spec:** `docs/superpowers/specs/2026-10-23-pi-supervisor-design.md`

## Global Constraints

- Python 3.9+，标准库；不增加 Python 或 Node 依赖。
- `AGENT_DISPATCH_WORKER` 和显式 `AGENT_DISPATCH_SESSION` 保持最高优先级；真实 pi 的 `PI_SESSION_ID` 在继承的 Claude/Codex 身份之前识别，pi 使用 `kind: "pi"`。
- 每张单首次派出时绑定主管 session ID，续派不得因当前操作者改变而换目标。
- Codex 的 `codex_notify` 记录、命令与行为向后兼容；pi 使用独立的 `pi_notify` 记录，状态输出应可诊断。
- 人工持有、叫停、已裁决、过时回合不触发自动处理。
- `AGENT_DISPATCH_PI_NOTIFY=0` 可以关闭；未装扩展时清楚提示手动 wait，不能谎报已被主管消费。
- 本次不改认证、模型池、数据库、用户全局 pi 配置，不连接业务环境，不 push。
- 所有测试使用临时派工台账并移除真实主管身份与通知路径，不能向本会话或 Codex 真会话发送测试消息。

## Review Focus

- pi 嵌套运行时继承宿主的 Claude/Codex 环境，身份仍指向实际 pi 会话。
- 主管切换/恢复 session，旧结果不得误发到新 session；未处理事件能保留或有明确的可恢复状态。
- 派单瞬间就完成，以及不同终端续派同一单，都不能遗漏或改换接收者。
- 投递期间发生 take/stop/accept，消费时应重新核对状态并抑制重复裁决。
- 本机真实 pi 身份、扩展与通知在测试子进程里不能泄漏，尤其独立 Popen 入口。

---

### Task 1: pi 身份与原会话回传完整能力

**Files:**
- Modify: `agent_dispatch/store.py`, `agent_dispatch/notify.py`, `agent_dispatch/cli.py`, `tests/test_dispatch.py`
- Create if needed: `agent_dispatch/pi_notify.py`
- Create: `.pi/extensions/agent-dispatch-supervisor.ts`
- Create: focused Python/Node extension tests under `tests/`
- Modify: `README.md`, `SUPERVISOR.md`, `SKILL.md`

**Interfaces:**
- Consumes: `store.identify_dispatcher(env, ppid, load_job=None)`、`notify.ensure_waiter(name)`、`notify.turn_action(job, turn)` 与现有 `codex_notify`。
- Produces: `kind: "pi"` 主管记录、绑定原 session 的每轮 `pi_notify`，及能够独立加载的 pi 原生通知扩展。
- ExtensionAPI: `ctx.sessionManager.getSessionId()` 获得真实会话；`pi.sendMessage(message, { triggerTurn: true, deliverAs: "followUp" })` 继续；`session_start` 建监听，`session_shutdown` 清理。轻量私有 CLI 的具体接口可按实现需要最小确定，并记录在报告。

- [ ] **Step 1: 建立基线与阅读 API**

先读 spec。查阅现有 store/notify/cli/test_dispatch 的对应入口。运行 `python3 -m unittest discover -s tests -p test_dispatch.py -k DispatcherSessionTest` 和 `... -k CodexNotifyTest`，保存日志。

pi 文档根 `/Users/wqd1994/.pi/agent/install/releases/1.4.0/node_modules/@earendil-works/pi-coding-agent`：完整读 `docs/extensions.md`、`docs/environment-variables.md`、用到的相关 .md 与最小 examples，精确 API 见 `dist/core/extensions/types.d.ts`。不得猜测存在 pi queue 命令。

- [ ] **Step 2: 写身份与通知集成失败测试，运行并保留 RED**

身份断言 `PI_SESSION_ID` => pi，worker/custom 优先，pi 高于继承的 Claude/Codex，空/非法 ID 不启用回传。集成测试覆盖 start/headless/tmux/reply、原 session 绑定、快速完成、关闭开关、投递失败/重试、去重、过时/held/stopped/accepted/rejected 与跨 session 隔离。扩充测试环境清理，默认禁用两种真实通知；独立异步 start 入口同样清理 pi 身份。

- [ ] **Step 3: 实现最小 Python 行为并运行 GREEN**

复用当前的轮次等待、锁、投递状态检查；加 pi 传输与独立 pi_notify。不要重构角色环境/模型池，不改变 codex_notify 的结构或语义。状态、错误、重试与扩展缺失情况应能从 CLI 诊断。对通知中断的不确定结果避免盲目重复投递。

- [ ] **Step 4: 写并运行真实扩展行为失败测试，然后实现到通过**

执行实际 TypeScript 扩展的 harness（可用已安装的 jiti 或 Node 的类型剥离，不加依赖）。验证 idle triggerTurn、busy followUp、重复事件、重载恢复、session 切换、shutdown 清理及 worker/print 模式不开监听。覆盖事件发布与扩展接收的 Python/TypeScript 边界，确保不是只 mock 出一个 PASS。监听期间不调用模型。

- [ ] **Step 5: 更新说明并完成检查**

README/SUPERVISOR/SKILL 写清仓库目录启动 pi 与 `/reload`，外部 `pi -e <绝对扩展路径>` 加载，模型不轮询等待，未加载扩展时手动 wait，关闭开关，以及本机/重载/投递状态限制。若有可行的无模型真实 pi 扩展加载烟测，隔离运行并留证；无法执行的界限如实报告。

- [ ] **Step 6: 最终测试、自查、提交、报告**

在本工作树运行 `python3 -m unittest discover -s tests`，保存完整日志，报告总数/失败/错误/跳过；专项与扩展 harness 必须通过。只提交自己的文件以及本次新建的设计和计划，不提交临时输出，不 push。原工作区、全局配置、真实通知路径不可修改。

报告写入主管派工单指定的 report 文件，含 RED/GREEN 命令与关键原始输出、改动清单、完整测试日志路径、完整计数、提交号、局限与未验证项。最终回复使用派工角色规定的 STATUS/SUMMARY/EVIDENCE/QUESTION 标签。
