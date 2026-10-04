"""后台运行器：每一轮派工对应一个脱离终端的运行器进程。

两种运行方式：
- headless：启动智能体的无头进程，读它标准输出上的事件流，进程退出即回合结束。
- tmux：智能体的交互界面跑在 tmux 会话里（你可以接进去看和打字），运行器读智能体的
  会话存储来跟进度、判断回合结束；回合结束后界面继续活着，浏览器和页面状态不丢。

两种方式都把过程落成同样的文件（归一化事件、可读日志、结论），结束时更新台账。
主管和网页只读这些文件，不和运行器通信。
"""
import datetime as dt
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time

from . import backends, store, tmux

STATUS_RE = re.compile(r"^[\s>*#-]*STATUS\**\s*[:：]\s*\**\s*([A-Za-z_]+)\**[ \t]*$", re.M)
FIELD_RE = re.compile(r"^[\s>*-]*([A-Z][A-Z_]{2,})\**\s*[:：]\s*(.*)$", re.M)
NOISE = ("agentmemory: ", "Warning: No project session found")
POLL_SECONDS = 2
SUBMIT_RETRY_SECONDS = 30   # 粘贴后这么久界面还没收到提示，补按一次回车
SUBMIT_GIVE_UP_SECONDS = 90


def clip(value, limit):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = text.replace("\n", "⏎")
    return text if len(text) <= limit else text[:limit] + "…"


def parse_report(text):
    """从智能体最后一条回复里取 STATUS 和其它 `KEY: 值` 字段（取最后一次出现）。"""
    statuses = STATUS_RE.findall(text or "")
    fields = {}
    for key, value in FIELD_RE.findall(text or ""):
        fields[key] = value.strip().strip("`*").strip()
    fields.pop("STATUS", None)
    return (statuses[-1].upper() if statuses else None), fields


def spawn(name):
    """由 CLI 调用：把运行器作为独立会话启动后立即返回。"""
    env = dict(os.environ)
    env["PYTHONPATH"] = backends.ROOT + os.pathsep + env.get("PYTHONPATH", "")
    with open(store.job_path(name, "runner.log"), "a", encoding="utf-8") as log:
        proc = subprocess.Popen([sys.executable, "-m", "agent_dispatch", "_run", name], env=env,
                                stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    return proc.pid


class Recorder:
    """把归一化事件写进 events.jsonl 和可读的 run.log。"""

    def __init__(self, name, turn):
        self.turn = turn
        self.errors = []
        self.final = ""
        self.events = open(store.job_path(name, "events.jsonl"), "a", encoding="utf-8")
        self.log = open(store.job_path(name, "run.log"), "a", encoding="utf-8")

    def say(self, line):
        self.log.write(f"[{dt.datetime.now():%H:%M:%S}] {line}\n")
        self.log.flush()

    def record(self, event):
        if event["kind"] == "user":
            self.say(f"输入  {clip(event.get('text') or '', 300)}")
            return
        event = dict(event, t=store.now(), turn=self.turn)
        self.events.write(json.dumps(event, ensure_ascii=False) + "\n")
        self.events.flush()
        if event["kind"] == "tool":
            took = f" {event['ms']}ms" if event.get("ms") is not None else ""
            self.say(f"工具  {event['name']} {clip(event.get('input'), 300)}")
            self.say(f"      └ {'失败' if event.get('error') else '完成'}{took}: {clip(event.get('output') or '', 300)}")
        elif event["kind"] == "text":
            self.final = event["text"]
            self.say("回复  " + event["text"].replace("\n", "\n            "))
        elif event["kind"] == "error":
            self.errors.append(event["message"])
            self.say(f"错误  {clip(event['message'], 1000)}")

    def close(self):
        self.events.close()
        self.log.close()


def settle(name, turn, recorder, result_state, reason, rc, final=None):
    """一轮结束：写结论、更新台账。人工持有期间结束的回合不抢状态。返回落定后的台账。"""
    final = recorder.final if final is None else final
    status, fields = parse_report(final)
    recorder.say(f"══ 第 {turn} 轮结束  状态={result_state} STATUS={status or '-'}" + (f"  {reason}" if reason else ""))
    recorder.close()
    store.write_text(store.job_path(name, "turns", f"{turn}.result.md"), final)
    store.write_text(store.job_path(name, "result.md"), final)
    with store.locked(name):
        job = store.load(name)
        job.setdefault("turns", []).append({"turn": turn, "started": job.get("turn_started"), "ended": store.now(),
                                            "rc": rc, "state": result_state, "status": status, "reason": reason})
        job.update(status=status, report=fields, reason=reason, exit_code=rc, ended=store.now(), ended_ts=time.time(),
                   runner_pid=None, child_pid=None, gate=None)
        if job.get("state") == "held":
            job["prev_state"] = result_state
        else:
            job["state"] = result_state
        store.save(job)
    return job


def install_stop(flags, on_stop):
    def handler(*_):
        flags["stopped"] = True
        on_stop()
    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


def run_headless(name, job, backend, task):
    turn = job["turn"]
    argv = backends.build_argv(backend, job, task, job.get("backend_args"))
    normalize = backends.NORMALIZERS[backend["format"]]
    flags = {"stopped": False, "timed_out": False}
    # PWD 必须跟着改：OpenCode 以环境变量 PWD 而不是进程的实际目录来认工作目录
    proc = subprocess.Popen(argv, cwd=job["cwd"], env=dict(os.environ, PWD=job["cwd"], AGENT_DISPATCH_WORKER=name),
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, start_new_session=True)
    store.update(name, child_pid=proc.pid)

    def kill_child():
        try:
            os.killpg(proc.pid, signal.SIGTERM)  # 连同它拉起的 MCP 服务、浏览器一起结束
        except ProcessLookupError:
            pass

    install_stop(flags, kill_child)
    timer = None
    if job.get("timeout_min"):
        def on_timeout():
            flags["timed_out"] = True
            kill_child()
        timer = threading.Timer(job["timeout_min"] * 60, on_timeout)
        timer.daemon = True
        timer.start()

    recorder = Recorder(name, turn)
    recorder.say(f"══ 第 {turn} 轮开始  方式=headless 后端={job['backend']} 模型={job['model']} 目录={job['cwd']}")
    state = {}
    with open(store.job_path(name, "turns", f"{turn}.raw.jsonl"), "a", encoding="utf-8") as raw:
        for line in proc.stdout:
            raw.write(line)
            raw.flush()
            try:
                obj = json.loads(line)
            except ValueError:
                if line.strip() and not line.startswith(NOISE):
                    recorder.say(f"输出  {line.rstrip()}")
                continue
            if not isinstance(obj, dict):
                continue
            for event in normalize(obj, state):
                if event["kind"] == "session":
                    if event.get("id") and event["id"] != job.get("session_id"):
                        job = store.update(name, session_id=event["id"])
                    continue
                recorder.record(event)
    rc = proc.wait()
    if timer:
        timer.cancel()
    kill_child()  # 智能体退出后，清掉可能残留的子进程

    final = state.get("final") or ""
    last_error = f"：{recorder.errors[-1]}" if recorder.errors else ""
    if flags["timed_out"]:
        result = ("failed", f"超过 {job['timeout_min']} 分钟被终止")
    elif flags["stopped"]:
        result = ("stopped", "被叫停")
    elif rc != 0:
        result = ("failed", f"进程退出码 {rc}{last_error}")
    elif not final:
        result = ("failed", f"智能体没有给出任何回复{last_error}")
    else:
        result = ("done", None)
    job = settle(name, turn, recorder, result[0], result[1], rc, final)
    store.release_channels(store.channel_ns(job), job.get("channels"), name)


def run_tmux(name, job, backend, task):
    """tmux 方式的一轮：把提示送进界面，然后读会话存储直到回合结束。界面在回合结束后保持存活。"""
    turn = job["turn"]
    session = job["tmux"]
    read = backends.SESSION_READERS[backend["format"]]
    flags = {"stopped": False}
    install_stop(flags, lambda: None)
    recorder = Recorder(name, turn)
    recorder.say(f"══ 第 {turn} 轮开始  方式=tmux（{tmux.attach_command(session)}） 后端={job['backend']} 模型={job['model']} 目录={job['cwd']}")

    following = bool(job.get("follow"))  # 只重新跟进一个还在跑的界面，不往里送任何内容
    # 基线必须读准：读不成就把旧消息当成新消息，会把上一轮的结束误判成这一轮的结束
    for _ in range(15):
        snapshot = read(backend, job)
        if snapshot is not None:
            break
        time.sleep(POLL_SECONDS)
    else:
        raise RuntimeError("读不到智能体的会话存储，无法开始跟进")
    items = snapshot[0]
    seen = len(items)
    users_before = sum(1 for item in items if item["kind"] == "user")
    if following:
        if not tmux.alive(session):
            raise RuntimeError("tmux 界面已经不在了，没有可跟进的回合")
    elif tmux.alive(session):
        tmux.paste(session, task)
        time.sleep(0.5)
        tmux.press_enter(session)
    else:
        tmux.new_session(session, job["cwd"], backends.build_interactive_argv(backend, job, task, job.get("backend_args")),
                         {"AGENT_DISPATCH_WORKER": name})
    sent_at = time.time()
    deadline = sent_at + job["timeout_min"] * 60 if job.get("timeout_min") else None
    retried = False
    received = following  # 一旦确认界面收到了提示就不再回头：之后某次读取失败不能推翻它

    while True:
        time.sleep(POLL_SECONDS)
        gone = not tmux.alive(session)
        snapshot = read(backend, job)
        idle = False
        if snapshot is not None:
            items, idle = snapshot
            for item in items[seen:]:
                recorder.record(item)
            seen = max(seen, len(items))
            received = received or sum(1 for item in items if item["kind"] == "user") > users_before
        if flags["stopped"]:
            tmux.kill(session)
            result = ("stopped", "被叫停")
        elif received and idle:
            result = ("done", None) if recorder.final or following else ("failed", "智能体没有给出任何回复" + (f"：{recorder.errors[-1]}" if recorder.errors else ""))
        elif gone:
            result = ("failed", "tmux 会话已结束（界面被关闭或崩溃），回合没有完成")
        elif deadline and time.time() > deadline:
            result = ("failed", f"超过 {job['timeout_min']} 分钟仍未结束；界面还开着，可接进去查看")
        elif not received and time.time() - sent_at > SUBMIT_GIVE_UP_SECONDS:
            result = ("failed", "派工单送进界面后一直没有被接收；接进去看看界面停在哪")
        else:
            if not received and not retried and time.time() - sent_at > SUBMIT_RETRY_SECONDS:
                tmux.press_enter(session)
                retried = True
            continue
        break

    final = recorder.final
    if following and not final:  # 跟进期间没有新回复：结论取会话里最后一条回复
        final = next((item["text"] for item in reversed(items) if item["kind"] == "text"), "")
    recorder.final = final
    job = settle(name, turn, recorder, result[0], result[1], None)
    if not tmux.alive(session):
        store.release_channels(store.channel_ns(job), job.get("channels"), name)


def run(name):
    try:
        job = store.update(name, runner_pid=os.getpid())
        task = store.read_text(store.job_path(name, "turns", f"{job['turn']}.task.md"), "")
        backend = backends.get(job["backend"])
        (run_tmux if job.get("mode") == "tmux" else run_headless)(name, job, backend, task)
    except Exception as exc:  # 启动失败（如找不到后端命令）也要落定，不能让作业悬在 running
        with store.locked(name):
            job = store.load(name)
            job.update(state="failed", status=None, reason=f"运行器出错：{exc}", ended=store.now(), ended_ts=time.time(),
                       runner_pid=None, child_pid=None, gate=None)
            store.save(job)
        if not tmux.alive(job.get("tmux")):
            store.release_channels(store.channel_ns(job), job.get("channels"), name)
        raise
