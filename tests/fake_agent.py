#!/usr/bin/env python3
"""测试用的假智能体：按 pi 或 opencode 的事件流格式输出，行为由任务文本里的指令控制。

  SLEEP <秒>       先睡这么久
  EXIT <码>        以该退出码结束
  SILENT           不输出最终回复
  MKRUN <目录名>   在 cwd/runs/ 下建 run 目录并写 report.md
  PWDCHECK         在回复里报告环境变量 PWD 是否等于实际工作目录
  FAILON <模型>     用这个模型时，一开始就报服务商错误（额度用尽）
  FAILAFTERTOOL <模型>  用这个模型时，执行过一次工具后才报服务商错误
  其余文本里的 `STATUS: X` 等行会原样作为最终回复
"""
import json
import os
import re
import sys
import time

fmt, session, task = sys.argv[1], sys.argv[2], sys.argv[3]
model = sys.argv[4] if len(sys.argv) > 4 else ""


def emit(obj):
    print(json.dumps(obj, ensure_ascii=False), flush=True)


if fmt != "pi":
    session = session or "ses_fake123"  # opencode 式：首轮由后端自己生成会话 ID

turns_file = os.path.join(os.getcwd(), f".fake-turns-{session}")
turn = int(open(turns_file).read()) + 1 if os.path.exists(turns_file) else 1
with open(turns_file, "w") as f:
    f.write(str(turn))

if f"FAILON {model}" in task.split("\n") or (model and f"FAILON {model}" in task):  # 模拟模型服务商拒绝（还没干活）
    emit({"type": "session", "id": session})
    emit({"type": "message_end", "message": {"role": "assistant", "content": [], "stopReason": "error",
                                             "errorMessage": "429 usage allocated quota exceeded"}})
    sys.exit(1)

if fmt == "pi":
    emit({"type": "session", "id": session})
    emit({"type": "tool_execution_start", "toolCallId": "c1", "toolName": "bash", "args": {"command": "echo hi"}})
    emit({"type": "tool_execution_end", "toolCallId": "c1", "toolName": "bash",
          "result": {"content": [{"type": "text", "text": "hi\n"}]}, "isError": False})
else:
    emit({"type": "step_start", "sessionID": session, "part": {"type": "step-start"}})
    emit({"type": "tool_use", "sessionID": session, "part": {"tool": "shell", "messageID": "m1", "state": {
        "status": "completed", "input": {"command": "echo hi"}, "output": "hi\n", "time": {"start": 1, "end": 43}}}})

match = re.search(r"SLEEP (\d+(?:\.\d+)?)", task)
if match:
    time.sleep(float(match.group(1)))

match = re.search(r"MKRUN (\S+)", task)
if match:
    run_dir = os.path.join(os.getcwd(), "runs", match.group(1))
    os.makedirs(run_dir, exist_ok=True)
    with open(os.path.join(run_dir, "report.md"), "w") as f:
        f.write("# 报告\n")

reply = "\n".join(line for line in task.splitlines() if re.match(r"^[A-Z_]+: ", line)) or "完成"
reply = f"第 {turn} 轮。\n{reply}"
if model and f"FAILAFTERTOOL {model}" in task:  # 干了一半才被服务商拒绝
    emit({"type": "message_end", "message": {"role": "assistant", "content": [], "stopReason": "error",
                                             "errorMessage": "429 usage allocated quota exceeded"}})
    sys.exit(1)
if "PWDCHECK" in task:  # 有的智能体按环境变量 PWD 认工作目录
    same = os.path.realpath(os.environ.get("PWD", "")) == os.path.realpath(os.getcwd())
    reply += f"\nPWD_OK: {'yes' if same else 'no'}"
if "INJECTDUMP" in task:  # 按作业下发 MCP / 技能的测试：报告拿到的环境变量与命令行
    reply += f"\nPI_DIR: {os.environ.get('PI_CODING_AGENT_DIR', '-')}"
    reply += f"\nOC_CFG: {os.environ.get('OPENCODE_CONFIG_CONTENT', '-')}"
    reply += f"\nARGV: {json.dumps(sys.argv[1:])}"
    reply += f"\nAD_ENV: {os.environ.get('AGENT_DISPATCH_ENV', '-')}"
if "NESTED" in task:  # 执行者试图再派工
    import subprocess
    nested = subprocess.run([sys.executable, "-m", "agent_dispatch", "start", "nested", "--cwd", os.getcwd(), "-b", "fakepi", "x"],
                            capture_output=True, text=True)
    reply += f"\nNESTED_RC: {nested.returncode}"
if "SILENT" not in task:
    if fmt == "pi":
        emit({"type": "message_end", "message": {"role": "assistant", "content": [{"type": "text", "text": reply}]}})
    else:
        emit({"type": "text", "sessionID": session, "part": {"messageID": "m2", "type": "text", "text": reply}})

match = re.search(r"EXIT (\d+)", task)
sys.exit(int(match.group(1)) if match else 0)
