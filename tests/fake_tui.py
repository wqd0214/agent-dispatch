#!/usr/bin/env python3
"""测试用的假交互式智能体：跑在 tmux 里，把对话写进 pi 格式的会话文件，回合结束后继续等输入。

用法：fake_tui.py <会话存储目录> <会话ID> <首条提示>
提示里的指令：SLEEP <秒>；其余 `KEY: 值` 行原样作为回复。
"""
import json
import os
import re
import sys
import time

sessions_dir, session, prompt = sys.argv[1], sys.argv[2], sys.argv[3]
path = os.path.join(sessions_dir, "proj", f"2026_{session}.jsonl")
os.makedirs(os.path.dirname(path), exist_ok=True)


def append(entry):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def message(role, **fields):
    append({"type": "message", "message": {"role": role, "timestamp": int(time.time() * 1000), **fields}})


if not os.path.exists(path):
    append({"type": "session", "id": session, "cwd": os.getcwd()})

turn = 0
while True:
    turn += 1
    print(f"> {prompt}", flush=True)
    message("user", content=[{"type": "text", "text": prompt}])
    message("assistant", stopReason="toolUse",
            content=[{"type": "toolCall", "id": f"c{turn}", "name": "bash", "arguments": {"command": "echo hi"}}])
    match = re.search(r"SLEEP (\d+(?:\.\d+)?)", prompt)
    if match:
        time.sleep(float(match.group(1)))
    message("toolResult", toolCallId=f"c{turn}", toolName="bash", content=[{"type": "text", "text": "hi\n"}], isError=False)
    reply = "\n".join(line for line in prompt.splitlines() if re.match(r"^[A-Z_]+: ", line)) or "完成"
    reply = f"第 {turn} 轮。\n{reply}"
    print(reply, flush=True)
    message("assistant", stopReason="stop", content=[{"type": "text", "text": reply}])
    prompt = ""
    while not prompt.strip():
        prompt = sys.stdin.readline()
        if not prompt:
            sys.exit(0)
    prompt = prompt.strip()
