"""pi 主管回传的本地文件传输：事件写到通知目录，由 pi 原生扩展消费后送入原会话。

目录布局（可诊断的最小私有契约，扩展与 CLI 都按它读写）：
  $AGENT_DISPATCH_HOME/notify/pi/<安全化的 session_id>/<event_id>.json          待消费事件
  $AGENT_DISPATCH_HOME/notify/pi/<安全化的 session_id>/<event_id>.sent.json     扩展已送入会话
  $AGENT_DISPATCH_HOME/notify/pi/<安全化的 session_id>/<event_id>.suppressed.json 扩展按台账重查后抑制

事件与标记都是 JSON；「sent / suppressed」由扩展原子改名后写入，Python 只读不写。
"""
import json
import os

from . import store

# 与 AGENT_DISPATCH_CODEX_NOTIFY 相互独立：关一个不影响另一个
KILL_SWITCH = "AGENT_DISPATCH_PI_NOTIFY"

# pi 记录的状态语义与 codex_notify 对齐；queued = 事件文件已写（不等于主管已消费）
STATE_CN = {"waiting": "等待回合结束", "delivering": "投递中", "queued": "已写入通知文件",
            "failed": "投递失败", "suppressed": "已抑制"}


def supervisor_session(job):
    """这张作业是否应做 pi 自动回传；返回目标 session id，否则 None。"""
    if os.environ.get(KILL_SWITCH) == "0":
        return None
    dispatcher = job.get("dispatcher") or {}
    if dispatcher.get("kind") != "pi":
        return None
    return store.valid_session_id(dispatcher.get("session_id"))


def find(job, turn):
    return next((r for r in job.get("pi_notify", []) if r["turn"] == turn), None)


def session_dir(session_id):
    return os.path.join(store.HOME, "notify", "pi", store.SAFE_ID_RE.sub("_", session_id))


def event_path(record):
    return os.path.join(session_dir(record["target"]), record["event_id"] + ".json")


def consumption(record):
    """扩展留下的消费标记：{'action': 'sent'|'suppressed', ...}；还没消费返回 None。"""
    base = event_path(record)[: -len(".json")]
    for suffix, action in ((".sent.json", "sent"), (".suppressed.json", "suppressed")):
        raw = store.read_text(base + suffix)
        if raw is None:
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            data = {}
        result = dict(data.get("consumed") or {})
        result.setdefault("action", action)
        return result
    return None


def deliver(name, turn, record):
    """调用者已把记录标成 delivering；把事件原子写入主监会话的通知目录。投递即写文件，没有「结果不明」态。"""
    with store.locked(name):
        job = store.load(name)
        from . import notify  # 延迟导入：notify 又 import 本模块
        payload = {
            "event_id": record["event_id"], "job": name, "turn": turn, "session_id": record["target"],
            "state": store.effective_state(job), "status": job.get("status"),
            "message": notify.message_for(job, record), "job_file": store.job_path(name, "job.json"),
            "ts": store.now(),
        }
    try:
        os.makedirs(session_dir(record["target"]), exist_ok=True)
        store.write_text(event_path(record), json.dumps(payload, ensure_ascii=False, indent=2))
    except OSError as exc:
        return "failed", f"无法写入 pi 通知文件：{exc}"
    return "queued", None
