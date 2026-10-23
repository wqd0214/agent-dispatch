"""状态目录：作业台账、文件锁、浏览器通道占用。全部是磁盘文件，没有服务。"""
import contextlib
import datetime as dt
import fcntl
import json
import os
import re
import sys
import uuid

from . import tmux

HOME = os.environ.get("AGENT_DISPATCH_HOME") or os.path.join(os.path.expanduser("~"), ".agent-dispatch")
JOBS_DIR = os.path.join(HOME, "jobs")
CHANNELS_DIR = os.path.join(HOME, "channels")
SESSIONS_DIR = os.path.join(HOME, "sessions")
BATCHES_DIR = os.path.join(HOME, "batches")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# 台账状态 → 中文（命令行与结果通知共用）
STATE_LABEL = {"running": "运行中", "queued": "排队中", "done": "待审查", "failed": "运行失败", "stopped": "已叫停",
               "interrupted": "意外中断", "held": "人工持有", "accepted": "已验收", "rejected": "已驳回"}

# 一轮结束后的状态；running 之外都算「已落定」
SETTLED = {"done", "failed", "stopped", "interrupted", "held", "accepted", "rejected"}


class DispatchError(Exception):
    pass


def now():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def info(msg):
    print(f"[agent-dispatch] {msg}", file=sys.stderr)


def check_name(name):
    if not NAME_RE.match(name):
        raise DispatchError(f"作业名只能包含字母数字 . _ -，且不能以符号开头：{name!r}")


def job_dir(name):
    return os.path.join(JOBS_DIR, name)


def job_path(name, *parts):
    return os.path.join(job_dir(name), *parts)


def write_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def read_text(path, default=None):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except FileNotFoundError:
        return default


def exists(name):
    return os.path.isfile(job_path(name, "job.json"))


def load(name):
    raw = read_text(job_path(name, "job.json"))
    if raw is None:
        raise DispatchError(f"没有这个作业：{name}")
    return json.loads(raw)


def save(job):
    job["updated"] = now()
    write_text(job_path(job["name"], "job.json"), json.dumps(job, ensure_ascii=False, indent=2))


@contextlib.contextmanager
def locked(name):
    """串行化对同一作业台账的读改写；主管、运行器和你本人可能同时操作。"""
    os.makedirs(job_dir(name), exist_ok=True)
    with open(job_path(name, ".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def update(name, **fields):
    with locked(name):
        job = load(name)
        job.update(fields)
        save(job)
        return job


def alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


def effective_state(job):
    """台账写着 running 但运行器进程已不在，说明被意外中断（关机、被杀）。"""
    if job.get("state") == "running" and not alive(job.get("runner_pid")):
        return "interrupted"
    return job.get("state", "-")


def derive_project(cwd):
    """按工作目录推断 (项目, 工程)：工程 = 目录名（worktree 归到主工程），项目 = 它的上级目录名。

    台账里这两个字段叫 group / project（历史命名）：group 存项目名，project 存工程名。
    """
    root = (cwd or "").split("/.worktrees/")[0].rstrip("/")
    return os.path.basename(os.path.dirname(root)), os.path.basename(root)


def project_of(job):
    """作业所属的 (项目, 工程)。派出时已记在台账里；早先的作业按目录推断。"""
    group, project = derive_project(job.get("cwd"))
    return job.get("group") or group, job.get("project") or project


def all_jobs():
    jobs = []
    if os.path.isdir(JOBS_DIR):
        for name in os.listdir(JOBS_DIR):
            if exists(name):
                try:
                    jobs.append(load(name))
                except (ValueError, DispatchError):
                    continue
    jobs.sort(key=lambda j: j.get("created", ""), reverse=True)
    return jobs


# ---------- 事项（批次的标题与原因） ----------

def batch_path(batch):
    # 批次名可以是中文等任意文本，文件名做安全化（与会话登记同一套做法）
    return os.path.join(BATCHES_DIR, SAFE_ID_RE.sub("_", batch) + ".json")


def load_batch(batch):
    try:
        data = json.loads(read_text(batch_path(batch)) or "")
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def register_batch(batch, title=None, reason=None, dispatcher=None):
    """登记 / 更新事项：标题和原因只有显式给出非空值才写，不谱空已有的；派工者记第一张单的。"""
    if not batch:
        return None
    data = load_batch(batch) or {"id": batch, "title": "", "reason": "",
                                 "created": now(), "dispatcher": None}
    if title:
        data["title"] = title
    if reason:
        data["reason"] = reason
    if dispatcher and not data.get("dispatcher"):
        data["dispatcher"] = {"kind": dispatcher.get("kind"), "session_id": dispatcher.get("session_id")}
    write_text(batch_path(batch), json.dumps(data, ensure_ascii=False, indent=2))
    return data


def _count_bucket(state):
    if state in ("running", "held", "queued"):
        return "running"
    if state == "done":
        return "review"
    if state in ("accepted", "rejected", "stopped"):
        return "done"
    return "failed"


def batch_dispatcher_row(jobs):
    """事项的派工者：优先登记文件里的，退回作业台账里的；都没有则是未登记的历史批次。"""
    who = None
    for job in jobs:
        dispatcher = job.get("dispatcher")
        if dispatcher:
            who = dispatcher
            break
    if not who:
        return {"kind": "未登记", "session_id": "", "title": ""}
    data = load_session(who.get("session_id")) or {}
    return {"kind": who.get("kind") or "-", "session_id": who.get("session_id") or "",
            "title": who.get("title") or data.get("title") or ""}


def batch_rows(project=None):
    """按批次（事项）汇总作业；没有登记文件的批次标题为空，不报错。"""
    groups = {}
    for job in all_jobs():
        if job.get("batch"):
            groups.setdefault(job["batch"], []).append(job)
    rows = []
    for batch, jobs in groups.items():
        meta = load_batch(batch) or {}
        if project is not None:
            projects = {job.get("project_id") or project_of(job)[0] for job in jobs}
            if project not in projects:
                continue
        counts = {"total": len(jobs), "running": 0, "review": 0, "done": 0, "failed": 0}
        for job in jobs:
            counts[_count_bucket(effective_state(job))] += 1
        jobs.sort(key=lambda j: (j.get("created") or "", j["name"]))  # 按派出时间正序
        dispatcher = meta.get("dispatcher") or batch_dispatcher_row(jobs)
        if meta.get("dispatcher") and not meta["dispatcher"].get("title"):
            session = load_session(meta["dispatcher"].get("session_id")) or {}
            dispatcher = dict(meta["dispatcher"], title=session.get("title") or "")
        rows.append({"id": batch, "title": meta.get("title") or "", "reason": meta.get("reason") or "",
                     "created": meta.get("created"), "dispatcher": dispatcher, "counts": counts,
                     "activity": max(j.get("updated") or j.get("created") or "" for j in jobs),
                     "jobs": [j["name"] for j in jobs]})
    rows.sort(key=lambda r: r["activity"] or "", reverse=True)
    return rows


# ---------- 派工者与会话登记 ----------

SAFE_ID_RE = re.compile(r"[^A-Za-z0-9._-]+")


def valid_thread_id(value):
    try:
        return str(uuid.UUID(value)) if isinstance(value, str) else None
    except ValueError:
        return None


def identify_dispatcher(env, ppid, load_job=None):
    """纯函数：按环境变量识别当前是谁在派工/裁决。load_job 只在执行者嵌套派工时用来查父作业的会话标识。

    优先级：执行者 > 显式自设会话 > Claude Code > Codex 原会话 > 终端。Codex 只认
    CODEX_THREAD_ID / CODEX_SESSION_ID 里的原会话 thread UUID，终端标题、PPID 一律不当 thread。
    """
    def out(kind, session_id, parent_job=None):
        return {"kind": kind, "session_id": session_id, "title": None, "cwd": None, "parent_job": parent_job}
    worker = env.get("AGENT_DISPATCH_WORKER")
    if worker:
        session_id = None
        if load_job:
            try:
                session_id = (load_job(worker) or {}).get("session_id")
            except (DispatchError, ValueError, OSError):
                session_id = None
        return out("执行者", session_id or f"job:{worker}", worker)
    if env.get("AGENT_DISPATCH_SESSION"):  # 任何主管工具都可以自设
        return out(env.get("AGENT_DISPATCH_SESSION_KIND") or "主管", env["AGENT_DISPATCH_SESSION"])
    if env.get("CLAUDE_CODE_SESSION_ID"):
        return out("Claude Code", env["CLAUDE_CODE_SESSION_ID"])
    thread = valid_thread_id(env.get("CODEX_THREAD_ID") or env.get("CODEX_SESSION_ID"))
    if thread:
        return out("Codex", thread)
    if env.get("TMUX_PANE"):  # 带 tmux 服务器标识，避免不同服务器的窗格编号重复
        server = os.path.basename((env.get("TMUX") or "default").split(",")[0])
        return out("终端", f"tmux:{server}:{env['TMUX_PANE']}")
    if env.get("TERM_SESSION_ID"):
        return out("终端", env["TERM_SESSION_ID"])
    return out("终端", f"ppid-{ppid}")


def who_dispatches():
    """当前命令的派工者身份；cwd 记命令执行时所在的目录。"""
    who = identify_dispatcher(os.environ, os.getppid(), load)
    who["cwd"] = os.getcwd()
    return who


def session_path(session_id):
    return os.path.join(SESSIONS_DIR, SAFE_ID_RE.sub("_", session_id) + ".json")


def load_session(session_id):
    try:
        data = json.loads(read_text(session_path(session_id)) or "")
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def touch_session(who, title=None):
    """每次派工或裁决时刷新会话登记的 last_seen；标题只有显式给出才写。"""
    if not who or not who.get("session_id"):
        return
    timestamp = now()
    data = load_session(who["session_id"])
    if data is None:
        data = {"id": who["session_id"], "kind": who.get("kind"), "title": None,
                "cwd": who.get("cwd"), "first_seen": timestamp, "last_seen": timestamp}
    else:
        data["last_seen"] = timestamp
        data["cwd"] = who.get("cwd") or data.get("cwd")
    if title:
        data["title"] = title
    write_text(session_path(who["session_id"]), json.dumps(data, ensure_ascii=False, indent=2))


def session_rows():
    """按派工会话汇总所有作业；没有 dispatcher 的历史作业归到一行「（未登记）」。"""
    groups = {}
    for job in all_jobs():
        groups.setdefault((job.get("dispatcher") or {}).get("session_id") or "", []).append(job)
    rows = []
    for session_id, jobs in groups.items():
        counts = {"total": len(jobs), "running": 0, "review": 0, "done": 0, "failed": 0}
        projects, batches = set(), []
        for job in jobs:
            state = effective_state(job)
            if state in ("running", "held", "queued"):
                counts["running"] += 1
            elif state == "done":
                counts["review"] += 1
            elif state in ("accepted", "rejected", "stopped"):
                counts["done"] += 1
            else:
                counts["failed"] += 1
            projects.add(job.get("project_id") or project_of(job)[0])
            if job.get("batch") and job["batch"] not in batches:
                batches.append(job["batch"])
        activity = max(j.get("updated") or j.get("created") or "" for j in jobs)
        if not session_id:
            row = {"id": "", "kind": "未登记", "title": batches[0] if batches else "", "cwd": None,
                   "first_seen": None, "last_seen": None}
        else:
            data = load_session(session_id) or {}
            row = {"id": session_id, "kind": data.get("kind") or (jobs[0].get("dispatcher") or {}).get("kind") or "-",
                   "title": data.get("title") or (jobs[0].get("dispatcher") or {}).get("title") or "",
                   "cwd": data.get("cwd"),
                   "first_seen": data.get("first_seen") or min(j.get("created") or "" for j in jobs),
                   "last_seen": max(filter(None, [data.get("last_seen")] + [j.get("created") or "" for j in jobs]))}
            activity = max(activity, row["last_seen"] or "")
        if not row["title"] and batches:
            row["title"] = batches[0]  # 界面退回用首个批次名
        row.update(projects=sorted(p for p in projects if p), counts=counts, total=len(jobs), activity=activity,
                   jobs=[j["name"] for j in jobs])
        rows.append(row)
    rows.sort(key=lambda r: r["activity"] or "", reverse=True)
    return rows


# ---------- 浏览器通道占用 ----------

def channel_ns(job):
    """通道锁的命名空间：同一个工程的所有角色共用一套通道。早先的作业用 profile 名。"""
    return job.get("channel_ns") or job["profile"]


def _channel_file(profile, channel):
    return os.path.join(CHANNELS_DIR, profile, f"{channel}.json")


def channel_holder(profile, channel):
    """返回占用该通道的作业名；占用者已不在运行也未被人工持有时视为空闲。"""
    raw = read_text(_channel_file(profile, channel))
    if not raw:
        return None
    try:
        holder = json.loads(raw).get("job")
    except ValueError:
        return None
    if not holder or not exists(holder):
        return None
    job = load(holder)
    # tmux 方式下回合结束后界面（连同它的浏览器）还活着，通道仍算被占用
    if effective_state(job) in ("running", "held") or tmux.alive(job.get("tmux")):
        return holder
    return None


def acquire_channels(profile, channels, name):
    """要么全部占到，要么一个都不占。"""
    if not channels:
        return
    os.makedirs(os.path.join(CHANNELS_DIR, profile), exist_ok=True)
    with open(os.path.join(CHANNELS_DIR, profile, ".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        busy = {}
        for ch in channels:
            holder = channel_holder(profile, ch)
            if holder and holder != name:
                busy[ch] = holder
        if busy:
            detail = "，".join(f"{ch} 被 {holder} 占用" for ch, holder in busy.items())
            raise DispatchError(f"通道忙：{detail}。等它结束（tmux 方式的作业要验收、驳回或 stop 后才释放），"
                                "或用 --channels 指定不冲突的通道")
        for ch in channels:
            write_text(_channel_file(profile, ch), json.dumps({"job": name, "since": now()}, ensure_ascii=False))


def release_channels(profile, channels, name):
    for ch in channels or []:
        path = _channel_file(profile, ch)
        raw = read_text(path)
        try:
            if raw and json.loads(raw).get("job") == name:
                os.remove(path)
        except (ValueError, FileNotFoundError):
            pass


def channel_table(profile, channels):
    return {ch: channel_holder(profile, ch) for ch in channels}
