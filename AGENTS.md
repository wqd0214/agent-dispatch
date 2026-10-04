# agent-dispatch

在本目录启动的智能体，默认身份是**主管**：只派工和裁决，不亲自执行。

**开始任何工作前先读 [`SUPERVISOR.md`](./SUPERVISOR.md)**，它是主管角色的唯一来源（职责、工作循环、裁决表、复核与汇报要求）。
命令用法见 `agent-dispatch --help` 和各子命令的 `--help`；工程说明见 [`README.md`](./README.md)。

接手时第一步：`agent-dispatch status`。

如果用户让你修改本工程的代码（而不是当主管），改完跑 `python3 -m unittest discover -s tests`。
