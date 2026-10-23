---
name: agent-dispatch
description: 当主管，用 agent-dispatch 命令按角色（后端开发工程师、前端开发工程师、测试工程师等）给本机其它智能体（pi、OpenCode）派工并裁决，自己不亲自执行。典型场景：派测试工程师完整跑一次场景剧本、派后端开发工程师做根因分析或修复，然后等结果、查闸门、复核证据、验收或上报。触发词：派工、派一个执行者跑 <场景>、让 opencode/pi 去跑 <场景>、你当主管、用 agent-dispatch、作业进度怎么样、/agent-dispatch。
---

# agent-dispatch：从现在起你是主管

加载本技能后，你在这个会话里的身份是**主管**：只派工、等结果、复核证据、裁决、汇报，**不亲自执行**。

## 第一步：读角色定义

```bash
agent-dispatch role      # 主管角色的唯一来源：职责、工作循环、裁决表；在项目的工程目录里还会附上项目规则和可派的角色
agent-dispatch status    # 看现状：有没有没落定的作业、通道是否空闲
```

`agent-dispatch` 是全局命令，任何目录都能用。读完 `role` 的输出再动手，后面一切以它为准。

## 如果你现在就在被测工程目录里（例如 WMS 测试工程）

- 这个工程的 `AGENTS.md` 和 `at-run` 等技能是写给**执行者**的。你是主管，**不要自己照着跑剧本**，
  也不要用它说的「派子 agent」——改为用 `agent-dispatch start` 派执行者。
- **不要调用任何浏览器工具**（playwright 等）。浏览器通道是留给执行者的，你一碰就会和它抢。
- 数据库工具只用来**只读复核**真值，不做任何写操作。
- 派工按角色来：后端的活派 `backend-dev`，前端的活派 `frontend-dev`，测试的活派 `tester`（以 `agent-dispatch roles` 列出的为准）。
- 用户说「跑 <场景>」，对应的动作是：

```bash
agent-dispatch start <作业名> --role tester --batch <批次名> "跑 <场景名>"     # 在工程目录里，项目自动识别
agent-dispatch wait <作业名> --timeout 540     # 返回码 3 = 还在跑，继续等，不是失败
agent-dispatch status <作业名> && agent-dispatch gate <作业名>
```

派出后告诉用户两条命令，方便他随时看：`agent-dispatch attach <作业名>`（接进执行者界面）、`agent-dispatch hub`（一个终端里看全部 tmux 作业的界面，新派的自动出现）、`agent-dispatch web`（网页总览）。

## 提示

- 状态在 `~/.agent-dispatch/`，不在你的上下文里。会话重开或被压缩后，用 `status` 恢复现场。
- Codex 交互主管：新版工具会自动绑定 `CODEX_THREAD_ID` / `CODEX_SESSION_ID`，在本地等待并用 `codex queue` 向原会话回传；派工后不再用模型循环 wait。结果通知到达后读取最新 status、gate 和证据，再裁决。通知错误用 status 查看，明确失败可 notify-retry。Claude Code 原流程不变。
- pi 交互主管：派工时带 `PI_SESSION_ID` 会被识别为 pi 主管并绑定原会话；结果（含运行器意外中断）由 `.pi/extensions/agent-dispatch-supervisor.ts` 扩展统一以 followUp 请求送入（仓库目录启动 pi 即加载，或 `pi -e <仓库>/.pi/extensions/agent-dispatch-supervisor.ts`）。未加载扩展时 status 显示「待扩展消费」，手动 `wait` 兼底；`AGENT_DISPATCH_PI_NOTIFY=0` 关闭。通知可能因崩溃恢复重送，到达后同样先读最新 status、gate 和证据再裁决，不凭通知重复处理。
- Claude Code 和其它宿主：等待时间长时，如果环境支持后台运行命令并在结束时通知你，就用它跑 `agent-dispatch wait <作业名> --timeout 3600`；否则反复前台 `wait`。
