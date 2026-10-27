"""主管原会话回传：本地等待作业结束，再向原会话投递结果；等待期间不调用模型。

Codex 主管用 ``codex queue`` 入队（记录在 codex_notify）；pi 主管把事件写到本地通知目录，
由 pi 原生扩展送入原会话（记录在 pi_notify，传输实现在 pi_notify）。两者共用轮次等待、
投递锁与投递前状态重查。"""
import contextlib
import fcntl
import os
import shutil
import subprocess
import sys
import time

from . import backends, pi_notify, store
from .store import DispatchError

STATE_CN = {"waiting": "等待回合结束", "delivering": "投递中", "queued": "已入队",
            "failed": "投递失败", "uncertain": "投递结果不明", "suppressed": "已抑制"}
DELIVERABLE = {"done", "failed", "interrupted"}
MAX_ATTEMPTS = 3


def _seconds(key, default, minimum):
    try:
        value = float(os.environ.get(key, default))
        return value if minimum <= value < float("inf") else default
    except ValueError:
        return default


def supervisor_thread(job):
    dispatcher = job.get("dispatcher") or {}
    if os.environ.get("AGENT_DISPATCH_CODEX_NOTIFY") == "0" or dispatcher.get("kind") != "Codex":
        return None
    return store.valid_thread_id(dispatcher.get("session_id"))


def resolve_codex():
    return shutil.which(os.environ.get("AGENT_DISPATCH_CODEX_BIN") or "codex")


def find(job, turn):
    return next((r for r in job.get("codex_notify", []) if r["turn"] == turn), None)


def record_and_transport(job, turn):
    """这一轮的通知记录和传输：先查 codex_notify，再查 pi_notify。"""
    record = find(job, turn)
    if record is not None:
        return record, "codex"
    record = pi_notify.find(job, turn)
    return record, ("pi" if record is not None else None)


def _update(name, turn, **fields):
    with store.locked(name):
        job = store.load(name)
        record = record_and_transport(job, turn)[0]
        record.update(fields, updated=store.now())
        store.save(job)
        return record


def turn_action(job, turn):
    state = store.effective_state(job)
    if job["turn"] != turn:
        return "suppress", "回合已过时"
    if state in {"held", "stopped", "accepted", "rejected"}:
        return "suppress", f"作业已{store.STATE_LABEL[state]}，不自动回传"
    return ("deliver", None) if state in DELIVERABLE else ("wait", None)


def message_for(job, record):
    state = store.effective_state(job)
    return "\n".join([
        f"[agent-dispatch] 作业 {job['name']} 第 {record['turn']} 轮已结束："
        f"{store.STATE_LABEL.get(state, state)}，STATUS={job.get('status') or '-'}",
        f"事件 {record['event_id']}。这是执行结果通知，尚未验收；请按既有主管规则复核。",
        f"复核：agent-dispatch status {job['name']}；闸门：agent-dispatch gate {job['name']}",
        f"结论：{store.job_path(job['name'], 'result.md')}  日志：{store.job_path(job['name'], 'run.log')}",
    ])


@contextlib.contextmanager
def _delivery_lock(name, turn):
    """只锁通知，不长期占用作业主锁；同一回合只允许一个等待/投递进程。"""
    if not store.exists(name):
        yield False
        return
    with open(store.job_path(name, f".notify-{turn}.lock"), "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _deliver(name, turn):
    """调用者持通知锁；投递前短暂持作业锁重查状态。"""
    with store.locked(name):
        job = store.load(name)
        record, transport = record_and_transport(job, turn)
        if record is None:
            return "failed", "没有通知记录"
        if record["state"] in {"queued", "suppressed"}:
            return record["state"], "已处理，不重复投递"
        if record["state"] in {"delivering", "uncertain"}:
            record.update(state="uncertain", error="上次投递可能已入队，请在原会话核对；不自动重发")
            store.save(job)
            return "uncertain", record["error"]
        action, reason = turn_action(job, turn)
        if action == "suppress":
            record.update(state="suppressed", reason=reason)
            store.save(job)
            return "suppressed", reason
        if action == "wait":
            return "waiting", "作业尚未结束"
        if record["attempts"] >= MAX_ATTEMPTS:
            return "failed", "已达到 3 次投递上限"
        record.update(state="delivering", owner=os.getpid(), attempts=record["attempts"] + 1, updated=store.now())
        store.save(job)
    state, error, receipt = "failed", None, ""
    if transport == "pi":
        # pi 传输：事件写入通知目录，等扩展消费；写成功即 queued，没有超时不确定态
        state, error = pi_notify.deliver(name, turn, record)
    else:
        executable = record.get("executable") or resolve_codex()
        if not executable:
            error = "找不到 codex；检查 PATH 或 AGENT_DISPATCH_CODEX_BIN 后可 notify-retry"
        else:
            try:
                result = subprocess.run([executable, "queue", "--thread", record["target"],
                                         "--message", message_for(job, record)],
                                        stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                        timeout=_seconds("AGENT_DISPATCH_CODEX_QUEUE_TIMEOUT", 30, 0.1))
                receipt = (result.stdout or "")[:1000]
                if result.returncode == 0:
                    state = "queued"
                else:
                    error = f"codex queue 退出码 {result.returncode}：{(result.stderr or result.stdout)[:1000]}"
            except subprocess.TimeoutExpired:
                state, error = "uncertain", "codex queue 超时；消息可能已入队，请在原会话核对，不自动重发"
            except OSError as exc:
                error = f"无法执行 codex：{exc}"
    queued_note = ("已写入通知文件，等待 pi 扩展送入会话（未确认已消费）" if transport == "pi"
                   else "已入队（未确认主管已消费）")
    _update(name, turn, state=state, error=error, receipt=receipt,
            note=queued_note if state == "queued" else None)
    return state, error or queued_note


def watch(name, turn):
    with _delivery_lock(name, turn) as acquired:
        if not acquired:
            return
        deadline = time.monotonic() + _seconds("AGENT_DISPATCH_CODEX_NOTIFY_TIMEOUT", 86400, 1)
        while store.exists(name):
            with store.locked(name):
                job = store.load(name)
                record, transport = record_and_transport(job, turn)
                if record is None or record["state"] != "waiting":
                    return
                action, reason = turn_action(job, turn)
                if action == "suppress":
                    record.update(state="suppressed", reason=reason, updated=store.now())
                    store.save(job)
                    return
            if action == "deliver":
                _deliver(name, turn)
                return
            if time.monotonic() >= deadline:
                _update(name, turn, state="failed", error="等待作业结束超时，可检查作业后 notify-retry")
                return
            time.sleep(_seconds("AGENT_DISPATCH_NOTIFY_POLL", 2, 0.05))


def ensure_waiter(name):
    """派工成功后尽力启动；通知启动失败不改变执行者的作业结果。"""
    try:
        with store.locked(name):
            job = store.load(name)
            target = supervisor_thread(job)
            transport = "codex"
            if not target:
                target = pi_notify.supervisor_session(job)
                transport = "pi" if target else None
            if not target:
                return
            turn = job["turn"]
            record = record_and_transport(job, turn)[0]
            if record is not None:
                if record["state"] != "waiting" or store.alive(record.get("owner")):
                    return
            else:
                record = {"turn": turn, "event_id": f"{transport}-{name}-t{turn}", "target": target,
                          "state": "waiting", "attempts": 0, "owner": None, "error": None,
                          "log": store.job_path(name, "notify.log" if transport == "codex" else "pi-notify.log"),
                          "created": store.now()}
                if transport == "codex":
                    record["executable"] = resolve_codex()
                records_key = "codex_notify" if transport == "codex" else "pi_notify"
                job.setdefault(records_key, []).append(record)
            try:
                env = dict(os.environ)
                env["PYTHONPATH"] = backends.ROOT + os.pathsep + env.get("PYTHONPATH", "")
                with open(record["log"], "a", encoding="utf-8") as log:
                    proc = subprocess.Popen([sys.executable, "-m", "agent_dispatch", "_notify", name, str(turn)],
                                            env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                            start_new_session=True)
                record.update(owner=proc.pid, updated=store.now())
            except OSError as exc:
                record.update(state="failed", error=f"无法启动通知等待器：{exc}")
            store.save(job)
    except (DispatchError, ValueError, OSError) as exc:
        store.info(f"⚠ 自动回传没有启动（不影响作业）：{exc}")


def retry_now(name, turn):
    with _delivery_lock(name, turn) as acquired:
        if not acquired:
            return "waiting", "已有通知等待器或投递进程，不重复启动"
        return _deliver(name, turn)
