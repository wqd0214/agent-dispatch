"""后端适配：每种智能体只需要「怎么启动/续聊」和「怎么读它的事件流」两样。

命令模板在 backends.json（可用 AGENT_DISPATCH_BACKENDS 指向别的文件）；
事件流格式各家不同，由这里的归一化函数转成统一事件：
  {"kind": "session", "id": ...}
  {"kind": "text", "text": ...}                      智能体的一段回复
  {"kind": "tool", "name", "input", "output", "error", "ms"}
  {"kind": "error", "message": ...}
"""
import glob
import json
import os
import re
import secrets
import subprocess
import tempfile
import time

from .store import DispatchError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def config_path():
    return os.environ.get("AGENT_DISPATCH_BACKENDS") or os.path.join(ROOT, "backends.json")


def load_all():
    with open(config_path(), encoding="utf-8") as f:
        return json.load(f)


def get(name):
    backends = load_all()
    if name not in backends:
        raise DispatchError(f"未知后端 {name!r}，可用：{', '.join(sorted(backends))}")
    backend = dict(backends[name])
    if backend.get("format") not in NORMALIZERS:
        raise DispatchError(f"后端 {name} 的 format={backend.get('format')!r} 没有对应的事件解析器")
    return backend


def initial_session(backend, job_name):
    """named：会话 ID 由我们指定；assigned：由后端生成，首轮从事件流里取回。

    ID 里带一段随机后缀：作业名被重用时不会续上旧会话的上下文。
    """
    if backend.get("session") != "named":
        return None
    template = backend.get("session_id") or "ad-{job}-{nonce}"
    return (template.replace("{jobkey}", re.sub(r"[^A-Za-z0-9]", "", job_name))
            .replace("{job}", job_name).replace("{nonce}", secrets.token_hex(3)))


def _fill(template, job, task, extra_args):
    if task.startswith("-"):
        task = "\n" + task  # 避免被后端命令行当成选项
    values = {"session": job.get("session_id") or "", "model": job["model"], "task": task}
    argv = []
    for token in template:
        if token == "{extra}":
            argv.extend(extra_args or [])
            continue
        for key, value in values.items():
            token = token.replace("{" + key + "}", value)
        argv.append(token)
    return argv


def build_argv(backend, job, task, extra_args=None):
    """无头方式一轮的命令行。"""
    if job.get("session_id"):
        template = backend["resume_argv"]
    elif job["turn"] > 1:
        raise DispatchError(f"作业 {job['name']} 没有会话 ID，无法续派（首轮可能没启动成功）")
    else:
        template = backend["argv"]
    return _fill(template, job, task, extra_args)


def build_interactive_argv(backend, job, task, extra_args=None):
    """tmux 方式：启动交互界面并带上首条提示。"""
    if not backend.get("interactive_argv") or not backend.get("session_reader"):
        raise DispatchError(f"后端 {job['backend']} 没有配置 interactive_argv / session_reader，不支持 tmux 方式")
    if not job.get("session_id"):
        raise DispatchError(f"后端 {job['backend']} 不能预先指定会话 ID，不支持 tmux 方式")
    return _fill(backend["interactive_argv"], job, task, extra_args)


def attach_command(backend, job):
    template = backend.get("attach")
    if not template or not job.get("session_id"):
        return None
    return template.replace("{session}", job["session_id"])


# ---------- 事件归一化 ----------

def _text_parts(content):
    if isinstance(content, str):
        return content
    return "\n".join(c.get("text", "") for c in content or [] if c.get("type") == "text").strip()


def normalize_pi(event, state):
    kind = event.get("type")
    if kind == "session":
        return [{"kind": "session", "id": event.get("id")}]
    if kind == "tool_execution_start":
        state.setdefault("tools", {})[event.get("toolCallId")] = (time.time(), event.get("args"))
        return []
    if kind == "tool_execution_end":
        started, args = state.get("tools", {}).pop(event.get("toolCallId"), (None, None))
        return [{
            "kind": "tool", "name": event.get("toolName"), "input": args,
            "output": _text_parts((event.get("result") or {}).get("content")),
            "error": bool(event.get("isError")),
            "ms": int((time.time() - started) * 1000) if started else None,
        }]
    if kind == "message_end":
        message = event.get("message") or {}
        if message.get("role") != "assistant":
            return []
        out = []
        if message.get("stopReason") == "error":
            out.append({"kind": "error", "message": message.get("errorMessage") or "模型调用出错"})
        text = _text_parts(message.get("content"))
        if text:
            state["final"] = text
            out.append({"kind": "text", "text": text})
        return out
    return []


def normalize_opencode(event, state):
    out = []
    session = event.get("sessionID")
    if session and state.get("session") != session:
        state["session"] = session
        out.append({"kind": "session", "id": session})
    kind = event.get("type")
    part = event.get("part") or {}
    if kind == "text":
        text = (part.get("text") or "").strip()
        if text:
            # 最终结论 = 最后一条消息里的全部文本段
            if state.get("final_message") != part.get("messageID"):
                state["final_message"] = part.get("messageID")
                state["final"] = text
            else:
                state["final"] = f"{state['final']}\n{text}"
            out.append({"kind": "text", "text": text})
    elif kind == "tool_use":
        tool = part.get("state") or {}
        span = tool.get("time") or {}
        ms = span["end"] - span["start"] if "end" in span and "start" in span else None
        out.append({
            "kind": "tool", "name": part.get("tool"), "input": tool.get("input"),
            "output": tool.get("output") if tool.get("output") is not None else tool.get("error"),
            "error": tool.get("status") == "error", "ms": ms,
        })
    elif kind == "error":
        out.append({"kind": "error", "message": (event.get("error") or {}).get("message") or json.dumps(event, ensure_ascii=False)})
    return out


NORMALIZERS = {"pi": normalize_pi, "opencode": normalize_opencode}


# ---------- 读会话存储（tmux 方式用） ----------
# 交互界面不往标准输出吐事件，所以改为读智能体自己的会话存储。
# 返回 (items, idle)：items 是只增不减的归一化条目（含 kind=user），idle 表示当前回合已结束。
# 返回 None 表示这次没读成（不是「会话为空」），调用方应跳过本次轮询，不能据此下任何结论。

def _ms(start, end):
    return int(end - start) if start is not None and end is not None else None


def read_session_pi(backend, job):
    path = _pi_session_path(backend, job)
    if not path:
        return [], False
    items, calls, idle = [], {}, False
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                entry = json.loads(line)
            except ValueError:
                continue  # 正在写入的半行
            if entry.get("type") != "message":
                continue
            message = entry.get("message") or {}
            role, stamp = message.get("role"), message.get("timestamp")
            if role == "user":
                items.append({"kind": "user", "text": _text_parts(message.get("content"))})
                idle = False
            elif role == "assistant":
                for part in message.get("content") or []:
                    if part.get("type") == "toolCall":
                        calls[part.get("id")] = (part.get("name"), part.get("arguments"), stamp)
                text = _text_parts(message.get("content"))
                if text:
                    items.append({"kind": "text", "text": text})
                stop = message.get("stopReason")
                if stop in ("error", "aborted"):
                    items.append({"kind": "error", "message": message.get("errorMessage") or f"回合被中止（{stop}）"})
                idle = stop not in (None, "toolUse", "pending")
            elif role == "toolResult":
                name, args, started = calls.pop(message.get("toolCallId"), (message.get("toolName"), None, None))
                items.append({"kind": "tool", "name": name, "input": args, "output": _text_parts(message.get("content")),
                              "error": bool(message.get("isError")), "ms": _ms(started, stamp)})
                idle = False
    return items, idle


def read_session_opencode(backend, job):
    argv = [token.replace("{session}", job["session_id"]) for token in backend["session_reader"]["argv"]]
    # 输出必须落到文件再读：经管道读时，会话一大（约 200KB 以上）导出内容会被截断成残缺的 JSON
    with tempfile.TemporaryFile() as out:
        result = subprocess.run(argv, cwd=job["cwd"], env=dict(os.environ, PWD=job["cwd"]), stdout=out,
                                stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        out.seek(0)
        raw = out.read().decode("utf-8", errors="replace")
    if not raw.strip():
        return ([], False) if result.returncode != 0 else None  # 非零且无输出 = 会话尚未建立
    try:
        messages = json.loads(raw).get("messages") or []
    except (ValueError, AttributeError):
        return None  # 这次没读成，不代表会话变了；调用方跳过本次
    items, idle = [], False
    for message in messages:
        kind = message.get("type")
        if kind == "user":
            items.append({"kind": "user", "text": message.get("text") or ""})
            idle = False
        elif kind == "assistant":
            if not (message.get("time") or {}).get("completed") and not message.get("error"):
                break  # 还在生成的消息下次再读，保证条目只增不改
            idle = False
            texts = []
            for part in message.get("content") or []:
                if part.get("type") == "text" and (part.get("text") or "").strip():
                    texts.append(part["text"].strip())
                elif part.get("type") == "tool":
                    state, span = part.get("state") or {}, part.get("time") or {}
                    output = _text_parts(state.get("content")) or state.get("error") or ""
                    items.append({"kind": "tool", "name": part.get("name"), "input": state.get("input"), "output": output,
                                  "error": state.get("status") == "error", "ms": _ms(span.get("ran"), span.get("completed"))})
            if texts:
                items.append({"kind": "text", "text": "\n".join(texts)})
            if message.get("error"):
                error = message["error"]
                text = error.get("message") if isinstance(error, dict) else None
                items.append({"kind": "error", "message": text or json.dumps(error, ensure_ascii=False)})
        elif kind == "idle":
            idle = True
            if message.get("outcome") not in (None, "succeeded"):
                items.append({"kind": "error", "message": f"回合结束，结果：{message.get('outcome')}"})
    return items, idle


SESSION_READERS = {"pi": read_session_pi, "opencode": read_session_opencode}


def _pi_session_path(backend, job):
    pattern = os.path.expanduser(backend["session_reader"]["glob"].replace("{session}", job["session_id"]))
    real = os.path.realpath(job["cwd"])
    for candidate in sorted(glob.glob(pattern), reverse=True):
        try:
            with open(candidate, encoding="utf-8") as f:
                if os.path.realpath(json.loads(f.readline()).get("cwd", "")) == real:
                    return candidate
        except (OSError, ValueError):
            continue
    return None


def session_model(backend, job):
    """会话里实际用的模型（provider/model）；读不到返回 None。用来发现「指定的模型没生效」。"""
    if not backend.get("session_reader") or not job.get("session_id"):
        return None
    if backend["format"] == "pi":
        path = _pi_session_path(backend, job)
        model = None
        for line in open(path, encoding="utf-8", errors="replace") if path else []:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if entry.get("type") == "model_change":
                model = f"{entry.get('provider')}/{entry.get('modelId')}"
        return model
    if backend["format"] == "opencode":
        argv = [token.replace("{session}", job["session_id"]) for token in backend["session_reader"]["argv"]]
        with tempfile.TemporaryFile() as out:
            subprocess.run(argv, cwd=job["cwd"], env=dict(os.environ, PWD=job["cwd"]), stdout=out,
                           stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
            out.seek(0)
            try:
                model = json.loads(out.read().decode("utf-8", errors="replace"))["info"]["model"]
                return f"{model['providerID']}/{model['id']}"
            except (ValueError, KeyError, TypeError):
                return None
    return None
