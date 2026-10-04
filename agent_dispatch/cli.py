"""agent-dispatch：纯命令行的多智能体派工。

主管（任意智能体会话或你本人）调用这些命令；执行者是后台的智能体进程
（无头，或跑在 tmux 会话里的交互界面）；状态全在 ~/.agent-dispatch/ 下的文件里。
"""
import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import unicodedata
import webbrowser

from . import backends, definitions, gates, inject, models, runner, store, tmux
from .store import DispatchError, identify_dispatcher, touch_session, who_dispatches

STATE_LABEL = {"running": "运行中", "queued": "排队中", "done": "待审查", "failed": "运行失败", "stopped": "已叫停",
               "interrupted": "意外中断", "held": "人工持有", "accepted": "已验收", "rejected": "已驳回"}
REPLYABLE = {"done", "failed", "stopped", "interrupted", "rejected"}
EXIT_TIMEOUT = 3


# ---------- profile ----------

def profile_dirs():
    return [os.path.join(store.HOME, "profiles"), os.path.join(backends.ROOT, "profiles")]


def load_profile(name):
    """按台账里记的配置名取配置：「项目:角色@工程」走项目/角色定义，其余是早先的 profile 文件。"""
    if not name or name == "default":
        return {"name": "default"}
    if ":" in name:
        return definitions.load(name)
    for directory in profile_dirs():
        path = os.path.join(directory, f"{name}.json")
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                profile = json.load(f)
            profile.update(name=name, _dir=directory)
            return profile
    raise DispatchError(f"找不到 profile {name!r}（查找位置：{'、'.join(profile_dirs())}）")


def select_config(a):
    """决定这次派工用哪份配置：早先的 profile，或（项目，角色，工程）。项目和工程可以从所在目录自动识别。"""
    if a.profile:
        return load_profile(a.profile)
    detected_project, detected_repo = definitions.detect(a.cwd or os.getcwd())
    if a.project:
        project = definitions.load_project(a.project)
        if not detected_project or detected_project["id"] != project["id"]:
            detected_repo = None
    else:
        project = detected_project
    if not project:
        if a.repo:
            raise DispatchError("--repo 要和项目一起用：加 --project，或在项目的某个工程目录里执行")
        return definitions.resolve(None, a.role) if a.role else {"name": "default"}
    role = a.role or project.get("default_role")
    if role:
        return definitions.resolve(project, role, a.repo, detected_repo)
    repo = a.repo or detected_repo or (next(iter(project["repos"])) if len(project["repos"]) == 1 else None)
    if not repo:
        raise DispatchError(f"项目 {project['id']} 有多个工程，请用 --role 指定角色或用 --repo 指明工程：{'、'.join(project['repos'])}")
    if repo not in project["repos"]:
        raise DispatchError(f"项目 {project['id']} 没有工程 {repo!r}，可选：{'、'.join(project['repos'])}")
    return definitions.bare(project, repo)


def render_brief(profile, task, job_name, channels, cwd):
    path = profile.get("_brief_path") or (os.path.join(profile["_dir"], profile["brief_template"]) if profile.get("brief_template") else None)
    if not path:
        return task
    text = store.read_text(path)
    if text is None:
        raise DispatchError(f"派工单模板不存在：{path}")
    values = {"task": task, "job": job_name, "channels": "、".join(channels) if channels else "（未分配浏览器通道）",
              "role": profile.get("title") or "", "project": profile.get("group") or "", "repo": profile.get("project") or "",
              "cwd": cwd}
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def backend_args(profile, backend_name):
    extra = profile.get("backend_args") or []
    return list(extra.get(backend_name) or []) if isinstance(extra, dict) else list(extra)


def pick_channels(profile, requested):
    available = profile.get("channels") or []
    choice = requested if requested is not None else profile.get("default_channels", "none")
    if choice == "none" or not available:
        return []
    if choice == "all":
        return list(available)
    picked = [c.strip() for c in choice.split(",") if c.strip()]
    unknown = [c for c in picked if c not in available]
    if unknown:
        raise DispatchError(f"这个工程没有通道 {'、'.join(unknown)}，可用：{'、'.join(available)}")
    return picked


# ---------- 通用 ----------

def read_task(args):
    if getattr(args, "file", None):
        text = store.read_text(args.file)
        if text is None:
            raise DispatchError(f"文件不存在：{args.file}")
        return text.strip()
    if args.task == ["-"] or (not args.task and not sys.stdin.isatty()):
        return sys.stdin.read().strip()
    return " ".join(args.task).strip()


def reconcile(job):
    """运行器意外消失时把台账落定为 interrupted，并清掉残留的智能体进程。"""
    if job.get("state") != "running" or store.alive(job.get("runner_pid")):
        return job
    with store.locked(job["name"]):
        job = store.load(job["name"])
        if job.get("state") != "running" or store.alive(job.get("runner_pid")):
            return job
        kill_group(job.get("child_pid"), signal.SIGTERM)
        job.update(state="interrupted", reason="运行器进程意外消失（关机或被杀）", ended=store.now(),
                   ended_ts=time.time(), runner_pid=None, child_pid=None)
        store.save(job)
    if not tmux.alive(job.get("tmux")):
        store.release_channels(store.channel_ns(job), job.get("channels"), job["name"])
    return job


def kill_group(pid, sig):
    if not pid:
        return
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def get_job(name):
    return reconcile(store.load(name))


def close_session(job):
    """作业不再需要执行者时：关掉 tmux 里的界面（连同浏览器），释放通道。"""
    tmux.kill(job.get("tmux"))
    if job.get("state") != "held":
        store.release_channels(store.channel_ns(job), job.get("channels"), job["name"])


def how_to_attach(job):
    if tmux.alive(job.get("tmux")) or (job.get("tmux") and job.get("state") == "running"):
        return tmux.attach_command(job["tmux"])  # 刚派出时界面可能还在启动
    backend = backends.get(job["backend"])
    attach = backends.attach_command(backend, job)
    if not attach:
        return None
    # 下发用的环境变量与参数要一并带上，接手后的会话才和执行者看到同样的 MCP / 技能
    prefix, suffix = inject.takeover_prefix(job["name"], backend, job)
    parts = ["cd", job["cwd"], "&&"]
    parts += ["env", *prefix] if prefix else []
    parts.append(attach)
    parts += suffix
    return " ".join(parts)


def decision_entry(job, action, note=None):
    """一条裁决记录：记下是谁做的（与派工者同一套识别逻辑）。"""
    who = who_dispatches()
    touch_session(who)
    return {"at": store.now(), "turn": job.get("turn"), "action": action, "note": note,
            "by": {"kind": who["kind"], "session_id": who["session_id"]}}


def decide(job, action, note=None):
    job.setdefault("decisions", []).append(decision_entry(job, action, note))


def elapsed(job):
    start = job.get("turn_started_ts")
    if not start:
        return "-"
    end = time.time() if job.get("state") == "running" else job.get("ended_ts") or start
    seconds = int(max(0, end - start))
    if seconds < 60:
        return f"{seconds}秒"
    if seconds < 3600:
        return f"{seconds // 60}分{seconds % 60:02d}秒"
    return f"{seconds // 3600}时{seconds % 3600 // 60:02d}分"


def width(text):
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def table(rows):
    widths = [max(width(str(row[i])) for row in rows) for i in range(len(rows[0]))]
    for row in rows:
        print("  ".join(str(cell) + " " * (w - width(str(cell))) for cell, w in zip(row, widths)).rstrip())


def launch(job, task_text):
    """占通道 → 写本轮派工单 → 启动运行器。占不到通道时不留下半截状态。"""
    name = job["name"]
    store.acquire_channels(store.channel_ns(job), job.get("channels"), name)
    store.write_text(store.job_path(name, "turns", f"{job['turn']}.task.md"), task_text)
    pid = runner.spawn(name)
    store.update(name, runner_pid=pid)
    return pid


# ---------- 命令 ----------

def cmd_start(a):
    worker = os.environ.get("AGENT_DISPATCH_WORKER")
    if worker and not a.dry_run:
        raise DispatchError(f"你是作业 {worker} 的执行者，不能再往下派工。做不了的事用 BLOCKED / NEEDS_DECISION 上报给主管")
    store.check_name(a.job)
    if store.exists(a.job):
        raise DispatchError(f"作业 {a.job} 已存在：续派用 reply，重新开始请换个作业名")
    profile = select_config(a)
    cwd = a.cwd or profile.get("cwd")
    if not cwd:
        raise DispatchError("没有工作目录：用 --project/--role 指定项目和角色，或显式给 --cwd")
    cwd = os.path.realpath(os.path.expanduser(os.path.expandvars(cwd)))
    if not os.path.isdir(cwd):
        raise DispatchError(f"工作目录不存在：{cwd}")
    model_alias = None
    pooled = bool(profile.get("model_pool"))
    if pooled:  # 模型由角色的模型池按比例决定，主管不能指定
        if a.model or a.backend:
            raise DispatchError(f"角色 {profile.get('title') or profile.get('role')} 的模型由它的模型池按比例决定，"
                                "不能用 --model / --backend 指定。要调整请改模型池（网页「模型与角色」或 agent-dispatch roles pool）")
        try:
            model_alias, picked = models.pick(profile["model_pool"], profile.get("project_id"), profile.get("role"))
            backend_name, model = picked["backend"], picked["id"]
        except models.ModelBusy as busy:  # 预览用：先拿一个在等的模型顶着，真正排不排队由锁里的复查决定
            backend_name, model = busy.waiting[0]
    else:
        backend_name = a.backend or profile.get("backend")
        if not backend_name:
            raise DispatchError("没有指定后端：用 --backend，或在角色定义里配置 backend")
        model = (a.model or (profile.get("model") if not a.backend else None)
                 or (profile.get("models") or {}).get(backend_name) or backends.get(backend_name).get("default_model"))
    backends.require_enabled(backend_name)
    backend = backends.get(backend_name)
    if not model:
        raise DispatchError(f"后端 {backend_name} 没有默认模型，请用 --model 指定")
    task = read_task(a)
    if not task:
        raise DispatchError("任务为空（用位置参数、-f 文件或标准输入传入）")
    inject_sel = inject.selection(profile)  # 角色管了 MCP / 技能时，派工命令要附带下发参数
    inject_desc = inject.describe(inject_sel, backend["format"])
    for followed in a.follows or []:  # 关联：这张单是因为那张单才派的
        if not store.exists(followed):
            raise DispatchError(f"--follows 的作业不存在：{followed}")
    channels = pick_channels(profile, a.channels)
    mode = a.mode or profile.get("mode") or "headless"
    session_id = backends.initial_session(backend, a.job)
    if mode == "tmux":
        if not tmux.available():
            raise DispatchError("本机没有 tmux，不能用 tmux 方式（改用 --mode headless）")
        backends.build_interactive_argv(backend, {"name": a.job, "backend": backend_name, "model": model,
                                                  "session_id": session_id}, task)
    brief = render_brief(profile, task, a.job, channels, cwd)
    if a.dry_run:
        if a.json:
            namespace = profile.get("channel_ns") or profile["name"]
            busy = {ch: holder for ch in channels
                    if (holder := store.channel_holder(namespace, ch))}
            print(json.dumps({
                "brief": brief, "job": a.job, "project": profile.get("group") or "",
                "repo": profile.get("project") or "", "role": profile.get("role"),
                "role_title": profile.get("title"), "backend": backend_name, "model": model,
                "model_alias": model_alias, "mode": mode, "cwd": cwd,
                "channels": channels, "channels_busy": busy,
                "inject": inject_desc,
            }, ensure_ascii=False))
        else:
            print(brief)
            if inject_desc["managed"]:
                print("\n## 本次下发\n")
                for line in inject.render(inject_desc):
                    print(line)
        return 0

    now_ts = time.time()
    group, project = store.derive_project(cwd)
    who = who_dispatches()
    if a.session_title:
        who["title"] = a.session_title
    touch_session(who, a.session_title)
    job = {
        "name": a.job, "batch": a.batch or "", "profile": profile["name"], "backend": backend_name, "model": model,
        "group": profile.get("group") or group, "project": profile.get("project") or project,  # 项目名 / 工程名
        "project_id": profile.get("project_id"), "repo": profile.get("repo"), "role": profile.get("role"),
        "role_title": profile.get("title"), "channel_ns": profile.get("channel_ns"),
        "model_alias": model_alias, "model_pool": profile.get("model_pool"), "model_attempts": [],
        "cwd": cwd, "session_id": session_id, "state": "running", "status": None,
        "dispatcher": who,
        "mode": mode, "tmux": "ad-" + a.job.replace(".", "_") if mode == "tmux" else None,
        "turn": 1, "channels": channels, "task": task, "follows": a.follows or [],
        "timeout_min": a.timeout if a.timeout is not None else profile.get("timeout_min"),
        "backend_args": backend_args(profile, backend_name),
        "inject": inject.summarize(inject_sel),
        "created": store.now(), "created_ts": now_ts, "turn_started": store.now(), "turn_started_ts": now_ts,
        "runner_pid": os.getpid(),  # 运行器接管前先用本进程占位，避免被判成意外中断
        "turns": [], "decisions": [],
    }
    # 占用检查与占位（写台账）在同一把锁里完成，并发派工时不会超发
    queued_waiting = None
    with models._locked():
        if pooled:  # 锁里重新选：早先的结果在并发下可能已失效
            try:
                model_alias, picked = models.pick(profile["model_pool"], profile.get("project_id"), profile.get("role"))
            except models.ModelBusy as busy:
                queued_waiting = busy.waiting
                backend_name, model = busy.waiting[0]
                model_alias = None
            else:
                backend_name, model = picked["backend"], picked["id"]
                session_id = backends.initial_session(backends.get(backend_name), a.job)
                if mode == "tmux":
                    backends.build_interactive_argv(backends.get(backend_name),
                                                    {"name": a.job, "backend": backend_name, "model": model,
                                                     "session_id": session_id}, task)
        elif models.busy(backend_name, model):
            queued_waiting = [(backend_name, model)]
        job.update(backend=backend_name, model=model, model_alias=model_alias,
                   backend_args=backend_args(profile, backend_name))
        if queued_waiting:
            job.update(state="queued", runner_pid=None,
                       waiting_models=[{"backend": b, "id": mid, "alias": models.alias_of(b, mid)} for b, mid in queued_waiting])
        store.save(job)
    store.write_text(store.job_path(a.job, "brief.md"), brief)
    if queued_waiting:
        store.write_text(store.job_path(a.job, "turns", f"{job['turn']}.task.md"), brief)
        who = f"角色={job['role_title']} 工程={job['project']}  " if job.get("role") else ""
        store.info(f"已派出 {a.job}  {who}方式={mode} 后端={backend_name} 模型={model} 通道={','.join(channels) or '无'}")
        store.info(f"模型并发已满，作业进入排队：等 {waiting_text(job)}。有模型空出来会自动开始，不需要重派")
    else:
        try:
            launch(job, brief)
        except DispatchError:
            shutil.rmtree(store.job_dir(a.job), ignore_errors=True)
            raise
        who = f"角色={job['role_title']} 工程={job['project']}  " if job.get("role") else ""
        picked_note = f"（按模型池选中 {model_alias}）" if model_alias else ""
        store.info(f"已派出 {a.job}  {who}方式={mode} 后端={backend_name} 模型={model}{picked_note} 通道={','.join(channels) or '无'}")
    if a.batch:
        meta = store.register_batch(a.batch, a.batch_title, a.batch_reason, job["dispatcher"])
        if not (meta or {}).get("title"):
            store.info(f"⚠ 这个批次还没有事项标题：请补上 agent-dispatch batch {a.batch} --title \"<事项标题>\" --reason \"<为什么派这批单>\""
                       "（或下次派工时带 --batch-title / --batch-reason）")
    store.info(f"等结果：agent-dispatch wait {a.job}    看输出：agent-dispatch log {a.job} -f"
               + (f"    接进界面：agent-dispatch attach {a.job}" if mode == "tmux" else ""))
    return 0


def cmd_reply(a):
    task = read_task(a)
    if not task:
        raise DispatchError("内容为空")
    queued = False
    with models._locked():  # 锁序与排队启动一致（先模型锁再作业锁），避免死锁
        with store.locked(a.job):
            job = reconcile_locked(a.job)
            state = job["state"]
            if state == "running":
                raise DispatchError(f"作业 {a.job} 还在运行：先 wait 等它结束，或 stop 叫停")
            if state == "held":
                raise DispatchError(f"作业 {a.job} 正被人工持有：先 release 交还")
            if state == "accepted" and not a.force:
                raise DispatchError(f"作业 {a.job} 已验收；确实要继续派请加 --force")
            if state not in REPLYABLE and not a.force:
                raise DispatchError(f"作业 {a.job} 当前状态 {state} 不能续派")
            profile = load_profile(job["profile"])
            now_ts = time.time()
            full_text = task + (profile.get("reply_suffix") or "")
            if models.busy(job["backend"], job["model"]):
                # 会话在那个模型上，满了就排队等，不换模型
                queued = True
                key = (job["backend"], job["model"])
                job.update(state="queued", status=None, reason=None, turn=job["turn"] + 1, gate=None, needs_user=None,
                           follow=False, turn_started=store.now(), turn_started_ts=now_ts, runner_pid=None,
                           waiting_models=[{"backend": key[0], "id": key[1], "alias": models.alias_of(*key)}])
                decide(job, "reply", task[:300])
                store.save(job)
                store.write_text(store.job_path(a.job, "turns", f"{job['turn']}.task.md"), full_text)
            else:
                job.update(state="running", status=None, reason=None, turn=job["turn"] + 1, gate=None, needs_user=None,
                           follow=False, turn_started=store.now(), turn_started_ts=now_ts, runner_pid=os.getpid())
                decide(job, "reply", task[:300])
                store.save(job)
    if queued:
        store.info(f"已续派 {a.job} 第 {job['turn']} 轮，但模型并发已满，排队中：等 {waiting_text(job)}。空出来会自动开始")
        return 0
    if not backends.enabled(job["backend"]):
        store.info(f"注意：派工工具 {job['backend']} 已停用。续派仍在原会话里进行；新派工不会再用它")
    try:
        launch(job, task + (profile.get("reply_suffix") or ""))
    except DispatchError as exc:
        store.update(a.job, state=state, turn=job["turn"] - 1, runner_pid=None, reason=f"续派未发出：{exc}")
        raise
    store.info(f"已续派 {a.job} 第 {job['turn']} 轮")
    return 0


def cmd_follow(a):
    """界面还在跑、但台账已落定（监视中断或误判）时，重新跟进：不往界面里送任何东西。"""
    with store.locked(a.job):
        job = reconcile_locked(a.job)
        if job.get("mode") != "tmux" or not tmux.alive(job.get("tmux")):
            raise DispatchError(f"作业 {a.job} 没有开着的 tmux 界面，无从跟进")
        if job["state"] in ("running", "held"):
            raise DispatchError(f"作业 {a.job} 当前是 {STATE_LABEL[job['state']]}，不需要跟进")
        now_ts = time.time()
        job.update(state="running", status=None, reason=None, turn=job["turn"] + 1, gate=None, follow=True,
                   turn_started=store.now(), turn_started_ts=now_ts, runner_pid=os.getpid())
        decide(job, "follow", a.note)
        store.save(job)
    launch(job, "（重新跟进仍在运行的界面，未发送新内容）")
    store.info(f"已重新跟进 {a.job}：界面里的回合结束时会正常落定")
    return 0


def reconcile_locked(name):
    """已持有作业锁时使用：只读出当前台账，意外中断的就地落定。"""
    job = store.load(name)
    if job.get("state") == "running" and not store.alive(job.get("runner_pid")):
        kill_group(job.get("child_pid"), signal.SIGTERM)
        job.update(state="interrupted", reason="运行器进程意外消失（关机或被杀）", ended=store.now(),
                   ended_ts=time.time(), runner_pid=None, child_pid=None)
        store.save(job)
        if not tmux.alive(job.get("tmux")):
            store.release_channels(store.channel_ns(job), job.get("channels"), name)
    return job


def resolve_targets(a):
    names = list(a.jobs)
    if getattr(a, "batch", None):
        names += [j["name"] for j in store.all_jobs() if j.get("batch") == a.batch and j["name"] not in names]
    if not names:
        raise DispatchError("没有指定作业（给作业名，或用 --batch）")
    return names


def summary_line(job):
    parts = [job["name"], STATE_LABEL.get(job["state"], job["state"]), f"STATUS={job.get('status') or '-'}",
             f"第{job['turn']}轮", elapsed(job)]
    if job.get("state") == "queued":
        parts[1] += f"（等 {waiting_text(job)}）"
    if job.get("reason"):
        parts.append(job["reason"])
    return "  ".join(parts)


def waiting_text(job):
    """排队中的作业在等哪些模型空出来，如「pi:glm-flash、opencode:glm-5」。"""
    return "、".join(f"{w.get('backend')}:{w.get('alias') or w.get('id')}" for w in job.get("waiting_models") or []) or "模型空位"


def cmd_wait(a):
    names = resolve_targets(a)
    deadline = time.time() + a.timeout
    while True:
        jobs = [get_job(n) for n in names]
        pending = [j for j in jobs if j["state"] in ("running", "queued")]
        if not pending or time.time() >= deadline:
            break
        time.sleep(min(2, max(0.1, deadline - time.time())))
    for job in jobs:
        print(summary_line(job))
    if pending:
        store.info(f"{len(pending)} 个作业还在运行或排队。这只是检查点，不是失败：继续 wait，或用 log 看进展")
        return EXIT_TIMEOUT
    return 0


def cmd_batches(a):
    """按事项（批次）汇总：每个事项一行，谁派的、各状态数量、最近活动。"""
    rows_data = store.batch_rows(a.project)
    if not rows_data:
        print("（暂无事项。派工时用 --batch 归组，并带 --batch-title / --batch-reason 说明事项）")
        return 0
    rows = [("事项", "原因", "派工者", "执行中", "待审查", "已完成", "失败", "最近活动", "批次")]
    for r in rows_data:
        c, d = r["counts"], r["dispatcher"]
        who = d["kind"] + (f"（{d['title']}）" if d.get("title") else "")
        rows.append((r["title"] or f"（未命名 {r['id']}）", r["reason"] or "-", who,
                     c["running"], c["review"], c["done"], c["failed"], r["activity"] or "-", r["id"]))
    table(rows)
    return 0


def cmd_batch(a):
    """查看一个事项：标题、原因、派工者和它名下的全部作业；也可事后补标题和原因。"""
    jobs = [j for j in store.all_jobs() if j.get("batch") == a.batch]
    meta = store.load_batch(a.batch)
    if not jobs and not meta:
        raise DispatchError(f"没有这个批次：{a.batch}")
    if a.title or a.reason:
        meta = store.register_batch(a.batch, a.title, a.reason)
        store.info(f"已更新事项 {a.batch} 的信息")
    dispatcher = (meta or {}).get("dispatcher") or store.batch_dispatcher_row(jobs)
    print(f"事项      {(meta or {}).get('title') or f'（未命名）'}   批次 {a.batch}")
    print(f"原因      {(meta or {}).get('reason') or '-'}")
    print(f"派工者    {dispatcher['kind']}  {dispatcher.get('title') or dispatcher.get('session_id') or ''}")
    if not jobs:
        print("（这个批次还没有作业）")
        return 0
    for job in sorted(jobs, key=lambda j: (j.get("created") or "", j["name"])):
        line = "  " + summary_line(job)
        if job.get("follows"):
            line += f"  （因 {'、'.join(job['follows'])} 而派）"
        print(line)
    return 0


def cmd_sessions(a):
    """按派工会话汇总：一次会话派了几张单、各是什么状态。"""
    rows_data = store.session_rows()
    if a.project:
        rows_data = [r for r in rows_data if a.project in r["projects"]]
    if not rows_data:
        print("（暂无作业）")
        return 0
    rows = [("类型", "标题", "项目", "总数", "执行中", "待审查", "已完成", "失败", "最近活动", "会话")]
    for r in rows_data:
        c = r["counts"]
        rows.append((r["kind"], r["title"] or "-", "、".join(r["projects"]) or "-", c["total"],
                     c["running"], c["review"], c["done"], c["failed"], r["activity"] or "-", r["id"] or "-"))
    table(rows)
    return 0


def print_status_table(batch=None, group=None, project=None):
    jobs = [reconcile(j) for j in store.all_jobs() if batch is None or j.get("batch") == batch]
    jobs = [j for j in jobs if (group is None or store.project_of(j)[0] == group)
            and (project is None or store.project_of(j)[1] == project)]
    if not jobs:
        print("（暂无作业）")
        return
    jobs.sort(key=lambda j: store.project_of(j))  # 同一项目的排在一起，组内仍是新的在前
    rows = [("项目/工程", "作业", "角色", "批次", "后端", "状态", "结论", "轮", "耗时", "通道", "任务")]
    for job in jobs:
        flag = " ⚑待你决定" if job.get("needs_user") else ""
        channels = job.get("channels") or []
        state = STATE_LABEL.get(job["state"], job["state"]) + flag
        if job.get("state") == "queued":
            state += f" · 等 {waiting_text(job)}"
        rows.append(("/".join(part for part in store.project_of(job) if part) or "-", job["name"], job.get("role_title") or "-",
                     job.get("batch") or "-", job["backend"],
                     state, job.get("status") or "-", job["turn"],
                     elapsed(job), (f"{len(channels)}个" if len(channels) > 3 else ",".join(channels)) or "-",
                     runner.clip(job.get("task", ""), 30)))
    table(rows)


def print_job_detail(job):
    print(f"作业      {job['name']}" + (f"   批次 {job['batch']}" if job.get("batch") else ""))
    if job.get("batch"):
        meta = store.load_batch(job["batch"]) or {}
        print(f"事项      {meta.get('title') or '（未命名）'}" + (f"：{meta['reason']}" if meta.get("reason") else ""))
    if job.get("follows"):
        print(f"因…而派  {'、'.join(job['follows'])}")
    followers = [j["name"] for j in store.all_jobs() if job["name"] in (j.get("follows") or [])]
    if followers:
        print(f"后续      {'、'.join(followers)}")
    print(f"项目      {' / '.join(part for part in store.project_of(job) if part) or '-'}"
          + (f"   角色 {job['role_title']}（{job['role']}）" if job.get("role") else ""))
    print(f"状态      {STATE_LABEL.get(job['state'], job['state'])}   STATUS={job.get('status') or '-'}   第 {job['turn']} 轮   耗时 {elapsed(job)}")
    if job.get("state") == "queued":
        print(f"排队      在等 {waiting_text(job)} 空出来；空出后自动开始，不需要重派")
    if job.get("reason"):
        print(f"原因      {job['reason']}")
    print(f"后端      {job['backend']}  {job['model']}" + (f"（{job['model_alias']}，按模型池选中）" if job.get("model_alias") else "")
          + (f"   ⚠ 实际用的是 {job['model_actual']}" if job.get("model_actual") else ""))
    for attempt in job.get("model_attempts") or []:
        print(f"  换过模型 [{attempt['at']}] {attempt['model']} → 原因：{attempt['reason']}")
    print(f"目录      {job['cwd']}")
    print(f"通道      {','.join(job.get('channels') or []) or '-'}")
    print(f"会话      {job.get('session_id') or '（尚未取得）'}")
    dispatcher = job.get("dispatcher")
    if dispatcher:
        print(f"派工者    {dispatcher['kind']}  {dispatcher.get('title') or dispatcher.get('session_id') or ''}")
    print(f"方式      {job.get('mode', 'headless')}" + (f"   界面{'开着' if tmux.alive(job.get('tmux')) else '已关闭'}" if job.get("tmux") else ""))
    if job.get("inject"):
        print("下发      " + "\n          ".join(inject.render(inject.describe_job(job))))
    attach = how_to_attach(job)
    if attach and tmux.alive(job.get("tmux")):
        print(f"接入      agent-dispatch attach {job['name']}   （即 {attach}；可直接看和打字）")
    elif attach:
        print(f"接手      agent-dispatch take {job['name']} 之后：{attach}")
    if job.get("run_dir"):
        print(f"run 目录  {job['run_dir']}")
    if job.get("needs_user"):
        print(f"⚑ 待你决定  {job['needs_user']['question']}")
    for key, value in (job.get("report") or {}).items():
        print(f"  {key}: {value}")
    if job.get("gate"):
        print_gate(job["gate"])
    for d in job.get("decisions") or []:
        print(f"  裁决 [{d['at']}] 第{d.get('turn')}轮 {d['action']}" + (f"：{d['note']}" if d.get("note") else ""))
    print(f"文件      {store.job_dir(job['name'])}")


def cmd_status(a):
    while True:
        if a.watch:
            print("\033[2J\033[H", end="")
            print(f"agent-dispatch  {store.now()}   （Ctrl-C 退出）\n")
        if a.json:
            jobs = [get_job(a.job)] if a.job else [reconcile(j) for j in store.all_jobs()]
            print(json.dumps(jobs if not a.job else jobs[0], ensure_ascii=False, indent=2))
        elif a.job:
            print_job_detail(get_job(a.job))
        else:
            print_status_table(a.batch, a.group, a.project)
        if not a.watch:
            return 0
        try:
            time.sleep(3)
        except KeyboardInterrupt:
            return 0


def cmd_log(a):
    get_job(a.job)
    path = store.job_path(a.job, "run.log")
    text = store.read_text(path, "")
    lines = text.splitlines()
    print("\n".join(lines[-a.n:]))
    if not a.follow:
        return 0
    offset = len(text.encode("utf-8"))
    try:
        while True:
            with open(path, "rb") as f:
                f.seek(offset)
                chunk = f.read()
            if chunk:
                offset += len(chunk)
                sys.stdout.write(chunk.decode("utf-8", errors="replace"))
                sys.stdout.flush()
            elif get_job(a.job)["state"] != "running":
                return 0
            time.sleep(1)
    except KeyboardInterrupt:
        return 0


def read_events(name):
    events = []
    for line in (store.read_text(store.job_path(name, "events.jsonl"), "") or "").splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    return events


def cmd_history(a):
    get_job(a.job)
    limit = None if a.full else 400
    for event in read_events(a.job)[-a.n:]:
        head = f"[{event.get('t', '')[11:]}] 第{event.get('turn')}轮"
        if event["kind"] == "tool":
            took = f" {event['ms']}ms" if event.get("ms") is not None else ""
            print(f"{head} 工具 {event.get('name')}{' ✗失败' if event.get('error') else ''}{took}")
            for label, value in (("入参", event.get("input")), ("返回", event.get("output"))):
                text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
                print(f"    {label}: {text if limit is None else runner.clip(text or '', limit)}")
        elif event["kind"] == "text":
            print(f"{head} 回复\n{event['text'] if limit is None else runner.clip(event['text'], limit)}")
        else:
            print(f"{head} 错误 {event.get('message')}")
    return 0


def stop_job(name):
    job = get_job(name)
    if job["state"] == "queued":  # 还没启动运行器，直接落定即可
        with store.locked(name):
            job = store.load(name)
            if job.get("state") == "queued":
                job.update(state="stopped", reason="被叫停（排队中，尚未开始）", ended=store.now(),
                           ended_ts=time.time(), waiting_models=None, runner_pid=None)
                store.save(job)
        return job, True
    if job["state"] != "running":
        return job, False
    runner_pid = job.get("runner_pid")
    try:
        os.kill(runner_pid, signal.SIGTERM)
    except (ProcessLookupError, TypeError):
        pass
    for _ in range(100):
        time.sleep(0.2)
        job = get_job(name)
        if job["state"] != "running":
            return job, True
    # 运行器没在 20 秒内收尾：强杀并由本命令落定
    kill_group(job.get("child_pid"), signal.SIGKILL)
    try:
        os.kill(runner_pid, signal.SIGKILL)
    except (ProcessLookupError, TypeError):
        pass
    job = store.update(name, state="stopped", reason="被叫停（强制结束）", ended=store.now(), ended_ts=time.time(),
                       runner_pid=None, child_pid=None)
    close_session(job)
    return job, True


def cmd_stop(a):
    job, stopped = stop_job(a.job)
    if not stopped:
        if tmux.alive(job.get("tmux")) and job["state"] != "held":
            close_session(job)
            inject.cleanup(a.job)
            store.info(f"作业 {a.job} 本轮已结束（{STATE_LABEL.get(job['state'], job['state'])}），已关闭它的界面并释放通道")
            return 0
        inject.cleanup(a.job)  # 下发文件里含凭据，叫停就删；续派时会重新生成
        store.info(f"作业 {a.job} 不在运行（{STATE_LABEL.get(job['state'], job['state'])}）")
        return 0
    store.update(a.job, decisions=(job.get("decisions") or []) + [decision_entry(job, "stop", a.reason)])
    inject.cleanup(a.job)  # 下发文件里含凭据，叫停就删；续派时会重新生成
    print(summary_line(job))
    return 0


def cmd_take(a):
    job = get_job(a.job)
    if job["state"] == "running" and (a.stop or job.get("mode") != "tmux"):
        if not a.stop:
            raise DispatchError(f"作业 {a.job} 还在运行：加 --stop 先叫停再接手，或等它结束")
        job, _ = stop_job(a.job)
    if job["state"] == "held":
        raise DispatchError(f"作业 {a.job} 已经是人工持有")
    attach = how_to_attach(job)
    with store.locked(a.job):
        job = store.load(a.job)
        previous = job["state"]
        job.update(state="held", prev_state=previous)
        decide(job, "take", a.note)
        store.save(job)
    try:
        store.acquire_channels(store.channel_ns(job), job.get("channels"), a.job)
    except DispatchError:
        store.update(a.job, state=previous, prev_state=None)
        raise
    store.info(f"{a.job} 已转为人工持有，主管不会再往里派工。通道 {','.join(job.get('channels') or []) or '无'} 归你使用")
    if attach:
        print(attach)
    store.info(f"用完后：agent-dispatch release {a.job}")
    return 0


def cmd_release(a):
    with store.locked(a.job):
        job = store.load(a.job)
        if job["state"] != "held":
            raise DispatchError(f"作业 {a.job} 不是人工持有状态")
        job.update(state=job.get("prev_state") or "done", prev_state=None)
        decide(job, "release", a.note)
        store.save(job)
    if not tmux.alive(job.get("tmux")):
        store.release_channels(store.channel_ns(job), job.get("channels"), a.job)
    store.info(f"{a.job} 已交还，当前状态：{STATE_LABEL.get(job['state'], job['state'])}")
    return 0


def print_gate(gate):
    print(f"闸门      {'通过' if gate['ok'] else '未通过'}（{gate['at']}）")
    for check in gate["checks"]:
        mark = "✓" if check["ok"] else ("✗" if check["level"] == "hard" else "⚠")
        print(f"  {mark} {check['gate']}：{check['detail']}")


def cmd_gate(a):
    job = get_job(a.job)
    if job["state"] == "running":
        raise DispatchError(f"作业 {a.job} 还在运行，结束后再查闸门")
    profile = load_profile(job["profile"])
    if not profile.get("gates"):
        store.info("这个作业的角色没有配置闸门")
        return 0
    run_dir, ok, checks = gates.run_gates(job, profile)
    gate = {"at": store.now(), "turn": job["turn"], "ok": ok, "checks": checks}
    store.update(a.job, gate=gate, run_dir=run_dir)
    print_gate(gate)
    return 0 if ok else 1


def cmd_accept(a):
    with store.locked(a.job):
        job = reconcile_locked(a.job)
        if job["state"] != "done" and not a.force:
            raise DispatchError(f"只有「待审查」的作业能验收；{a.job} 当前是 {STATE_LABEL.get(job['state'], job['state'])}")
        if load_profile(job["profile"]).get("gates") and not a.force:
            gate = job.get("gate")
            if not gate or gate.get("turn") != job["turn"]:
                raise DispatchError(f"还没对本轮跑过闸门：先 agent-dispatch gate {a.job}")
            if not gate["ok"]:
                raise DispatchError("闸门未通过，不能验收（确有理由请加 --force 并用 --note 写明）")
        if a.force and not a.note:
            raise DispatchError("--force 验收必须用 --note 写明原因")
        job.update(state="accepted", needs_user=None)
        decide(job, "accept" + ("(force)" if a.force else ""), a.note)
        store.save(job)
    close_session(job)
    inject.cleanup(a.job)  # 下发文件里含凭据，作业结束就删
    store.info(f"{a.job} 已验收")
    return 0


def cmd_reject(a):
    with store.locked(a.job):
        job = reconcile_locked(a.job)
        if job["state"] in ("running", "held"):
            raise DispatchError(f"作业 {a.job} 当前是 {STATE_LABEL[job['state']]}，不能驳回")
        job.update(state="rejected", needs_user=None)
        decide(job, "reject", a.reason)
        store.save(job)
    close_session(job)
    inject.cleanup(a.job)  # 下发文件里含凭据，作业结束就删
    store.info(f"{a.job} 已驳回")
    return 0


def cmd_escalate(a):
    with store.locked(a.job):
        job = store.load(a.job)
        if a.clear:
            job["needs_user"] = None
            decide(job, "escalate-clear", None)
        else:
            question = " ".join(a.question).strip()
            if not question:
                raise DispatchError("请写明需要用户决定什么")
            job["needs_user"] = {"question": question, "at": store.now()}
            decide(job, "escalate", question)
        store.save(job)
    store.info(f"{a.job} " + ("已清除待决标记" if a.clear else "已标记为等待用户决定"))
    return 0


def cmd_attach(a):
    job = get_job(a.job)
    if not tmux.alive(job.get("tmux")):
        hint = how_to_attach(job)
        raise DispatchError(f"作业 {a.job} 没有开着的 tmux 界面" + (f"。要接手它的会话：agent-dispatch take {a.job} 之后 {hint}" if hint else ""))
    if os.environ.get("TMUX"):
        os.execvp("tmux", ["tmux", "switch-client", "-t", job["tmux"]])
    os.execvp("tmux", ["tmux", "attach", "-t", job["tmux"]])


def current_project(a):
    """命令针对哪个项目：--project 指定，否则按当前目录识别。"""
    if getattr(a, "project", None):
        return definitions.load_project(a.project)
    return definitions.detect(os.getcwd())[0]


def role_table(project):
    rows = [("角色ID", "称呼", "工程", "模型·方式", "来源", "说明")]
    for role in definitions.list_roles(project):
        repos = "、".join(role["repos"]) or ("（派工时指定）" if project else "-")
        pool = " ".join(f"{e['model']}={e['weight']}" for e in role.get("model_pool") or [])
        rows.append((role["role"], role["title"], repos, f"{pool or role.get('backend') or '-'}·{role.get('mode') or 'headless'}",
                     role["scope"], runner.clip(role.get("description") or "", 44)))
    return rows


def cmd_role(a):
    """打印主管角色文档：任何目录、任何智能体都能用这条命令拿到同一份定义。在项目里会附上项目的补充规则和可派的角色。"""
    print(store.read_text(os.path.join(backends.ROOT, "SUPERVISOR.md"), ""))
    project = current_project(a)
    if not project:
        return 0
    print(f"\n---\n\n# 项目 {project['name']}（ID：{project['id']}）\n")
    notes = definitions.supervisor_notes(project)
    if notes:
        print(notes.strip() + "\n")
    print("## 工程\n")
    for repo in project["repos"].values():
        channels = f"   通道：{'、'.join(repo['channels'])}" if repo["channels"] else ""
        print(f"- `{repo['id']}`：{repo['cwd']}{channels}")
    print(f"\n## 可派的角色（agent-dispatch start <作业名> --project {project['id']} --role <角色ID> \"任务\"）\n")
    table(role_table(project))
    return 0


def parse_pool(pairs):
    pool = []
    for pair in pairs:
        alias, sep, weight = pair.rpartition("=")
        if not sep:
            raise DispatchError(f"写成 <模型短名>=<比例>，如 pi:glm-5.3-flash=70：{pair!r}")
        try:
            pool.append({"model": alias, "weight": float(weight) if "." in weight else int(weight)})
        except ValueError:
            raise DispatchError(f"比例必须是数字：{pair!r}")
    return pool


def model_table():
    registry, paused = models.load_registry(), models.health()
    counts = models.occupancy()
    rows = [("短名", "后端", "模型", "并发", "状态", "用在哪些角色", "备注")]
    users = {}
    for project_id, role_id, pool in models.all_pools():
        for entry in pool:
            users.setdefault(entry["model"], []).append(f"{project_id}:{role_id}={entry['weight']}")
    for alias, model in sorted(registry["models"].items()):
        state = ("停用" if not model.get("enabled", True) else f"{model['backend']} 已停用" if not backends.enabled(model["backend"])
                 else f"暂停至 {time.strftime('%H:%M', time.localtime(paused[alias]['until']))}" if alias in paused else "可用")
        key = (model["backend"], model["id"])
        concurrency = f"{counts.get(key, 0)}/{models.limit_for(key, registry)}"
        rows.append((alias, model["backend"], model["id"], concurrency, state,
                     "、".join(users.get(alias, [])) or "-", model.get("note") or ""))
    return rows


def cmd_models(a):
    action = a.action or "list"
    if action == "list":
        if not models.load_registry()["models"]:
            print("（模型登记表是空的。导入：agent-dispatch models import --list）")
            return 0
        table(model_table())
        return 0
    if action == "import":
        if a.backend:
            backends.get(a.backend)
        if a.list_candidates:  # 先看再选：列出工具里现有的模型及登记情况
            candidates = models.discover_candidates(a.backend)
            if not candidates:
                print("（没有从工具里发现模型）")
                return 0
            rows = [("后端", "模型 ID", "登记情况")]
            rows += [(c["backend"], c["id"], f"已登记（{c['alias']}）" if c["registered"] else "未登记") for c in candidates]
            table(rows)
            print("\n按名导入：agent-dispatch models import <模型ID …> [--backend …]；全部导入：agent-dispatch models import [--backend …]")
            return 0
        names = a.names + ([a.alias] if a.alias else [])  # 单个名字会落在 alias 位上
        if names:
            added = models.import_named(names, a.backend)
        else:
            added = models.import_all(a.backend)
        store.info(f"新登记 {len(added)} 个模型" + (f"：{'、'.join(added)}" if added else "（都已登记过）"))
        return 0
    if action == "limit":
        from . import runner
        if a.default_limit is not None:
            models.set_default_limit(a.default_limit)
            runner.promote_queued()  # 调大上限后，排队中的作业可能马上能言
            store.info(f"全局默认并发上限已设为 {a.default_limit}（没单独设置的模型用它）")
            return 0
        if not a.alias or not a.value:
            raise DispatchError("用法：agent-dispatch models limit <短名> <N|default>，或 models limit --default <N>")
        if a.value == "default":
            value = None
        else:
            try:
                number = int(a.value)
            except ValueError:
                raise DispatchError(f"并发上限必须是 ≥1 的整数或 default：{a.value!r}")
            value = models.check_limit(number)
        models.set_limit(a.alias, value)
        runner.promote_queued()
        store.info(f"模型 {a.alias} 的并发上限已" + ("恢复为全局默认值" if value is None else f"设为 {value}"))
        return 0
    if not a.alias:
        raise DispatchError(f"models {action} 需要模型短名")
    if action == "add":
        if not a.backend or not a.id:
            raise DispatchError("用法：agent-dispatch models add <短名> --backend pi --id <provider/model> [--note 备注]")
        models.add(a.alias, a.backend, a.id, a.note or "")
    elif action in ("enable", "disable"):
        models.update(a.alias, enabled=action == "enable")
    elif action == "note":
        models.update(a.alias, note=a.note or "")
    elif action == "resume":
        models.resume(a.alias)
    elif action == "remove":
        models.remove(a.alias)
    store.info(f"已处理：models {action} {a.alias}")
    return 0


def cmd_backends(a):
    action = a.action or "list"
    if action == "list":
        rows = [("派工工具", "状态", "默认模型", "登记的模型数")]
        registry = models.load_registry()["models"]
        for name, backend in sorted(backends.load_all().items()):
            rows.append((name, "可用" if backends.enabled(name) else "已停用", backend.get("default_model") or "-",
                         sum(1 for m in registry.values() if m["backend"] == name)))
        table(rows)
        return 0
    if not a.name:
        raise DispatchError(f"backends {action} 需要工具名，如 opencode")
    backends.set_enabled(a.name, action == "enable")
    store.info(f"派工工具 {a.name} 已{'启用' if action == 'enable' else '停用'}" +
               ("" if action == "enable" else "：新派工不会再用它，模型池里属于它的模型会被跳过；进行中的作业不受影响"))
    return 0


def cmd_projects(a):
    action, args = a.action or "list", a.args
    if action == "list":
        projects = definitions.list_projects()
        if not projects:
            print("（还没有定义项目。新建：agent-dispatch projects add <ID> --name <名字> --repo <工程名>=<目录>）")
            return 0
        rows = [("项目ID", "名称", "工程", "角色")]
        for project in projects:
            rows.append((project["id"], project["name"], "、".join(project["repos"]) or "-",
                         "、".join(r["title"] for r in definitions.list_roles(project)) or "-"))
        table(rows)
        return 0
    if not args:
        raise DispatchError(f"projects {action} 需要项目 ID")
    if action == "show":
        project = definitions.load_project(args[0])
        print(f"项目 {project['name']}（ID：{project['id']}）   定义：{definitions.project_dir(project['id'])}\n")
        rows = [("工程ID", "名称", "目录", "通道")]
        for repo in project["repos"].values():
            rows.append((repo["id"], repo["name"], repo["cwd"], "、".join(repo["channels"]) or "-"))
        table(rows)
        print()
        table(role_table(project))
        return 0
    if action == "add":
        repos = dict(pair.split("=", 1) for pair in a.repo or [])
        path = definitions.add_project(args[0], a.name, repos)
        store.info(f"已建项目 {args[0]}：{path}")
        return 0
    if action == "add-repo":
        if len(args) < 2 or "=" not in args[1]:
            raise DispatchError("用法：agent-dispatch projects add-repo <项目ID> <工程名>=<目录> [--channels a,b]")
        repo_id, cwd = args[1].split("=", 1)
        definitions.add_repo(args[0], repo_id, cwd, [c.strip() for c in a.channels.split(",")] if a.channels else None)
        store.info(f"已把工程 {repo_id} 加进项目 {args[0]}")
        return 0
    raise DispatchError(f"未知动作 {action}")


def mcp_table():
    from . import registry
    rows = [("登记 ID", "服务名", "类型", "命令 / URL", "被哪些角色用", "来源", "说明")]
    for reg_id, entry in sorted(registry.load_mcp().items()):
        masked = registry.mask_entry(entry)
        endpoint = masked.get("url") or " ".join([masked.get("command") or "-"] + (masked.get("args") or []))
        users = "、".join(f"{u['project'] or '全局'}:{u['role']}" for u in registry.used_by("mcp", reg_id)) or "-"
        rows.append((reg_id, entry.get("name") or reg_id, entry.get("type"), endpoint, users,
                     "、".join(entry.get("sources") or []) or "-", entry.get("description") or ""))
    return rows


def cmd_mcp(a):
    from . import registry
    action = a.action or "list"
    if action == "list":
        if not registry.load_mcp():
            print("（MCP 登记表是空的。导入现有配置：agent-dispatch mcp import）")
            return 0
        table(mcp_table())
        return 0
    if action == "import":
        groups = registry.mcp_candidates()
        if not a.all and not a.names:  # 不带参数：按服务名列出候选
            if not groups:
                print("（没有在 pi / OpenCode 配置里找到 MCP 服务）")
                return 0
            for group in groups:
                mark = "  ← 同名不同内容" if group["conflict"] else ""
                print(f"{group['name']}{mark}")
                rows = [("登记 ID", "来源", "差异（这组里不同的参数）", "已登记")]
                for v in group["variants"]:
                    diff = " ".join(f"{k}={registry.mask_any(val)}" for k, val in v["diff"].items()) or "（相同）"
                    rows.append((v["id"], "、".join(v["sources"]), diff, "是" if v["registered"] else "-"))
                table(rows)
                print()
            print("导入：agent-dispatch mcp import --all（全导入，同名各成一条）或 import <登记ID | 服务名> [--source <来源>]；"
                  "同名不同内容的要按登记 ID 或来源指明")
            return 0
        if a.all:
            selections = [v["id"] for group in groups for v in group["variants"] if not v["registered"]]
        else:
            selections = [{"name": n, "source": a.source} for n in a.names]
        added = registry.import_mcp(selections)
        store.info(f"新登记 {len(added)} 个 MCP 服务" + (f"：{'、'.join(added)}" if added else "（没有新的）"))
        return 0
    if not a.names:
        raise DispatchError(f"mcp {action} 需要登记 ID")
    if action == "add":
        registry.save_mcp(a.names[0], _mcp_entry_from_flags(a))
    elif action == "remove":
        registry.remove_mcp(a.names[0])
    store.info(f"已处理：mcp {action} {a.names[0]}")
    return 0


def _mcp_entry_from_flags(a):
    entry = {}
    if a.type:
        entry["type"] = a.type
    if a.command:
        entry["command"] = a.command
    if a.args:
        entry["args"] = [arg for group in a.args for arg in group.split(",") if arg]
    if a.env:
        entry["env"] = dict(pair.split("=", 1) for pair in a.env)
    if a.headers:
        entry["headers"] = dict(pair.split("=", 1) for pair in a.headers)
    if a.url:
        entry["url"] = a.url
    if a.description:
        entry["description"] = a.description
    if a.name:
        entry["name"] = a.name  # 下发给执行者的服务名，缺省等于登记 ID
    return entry


def cmd_skills(a):
    from . import registry
    action = a.action or "list"
    if action == "list":
        if not registry.load_skills():
            print("（技能登记表是空的。导入现有配置：agent-dispatch skills import）")
            return 0
        rows = [("登记 ID", "技能名", "目录", "被哪些角色用", "说明", "来源")]
        for reg_id, entry in sorted(registry.load_skills().items()):
            users = "、".join(f"{u['project'] or '全局'}:{u['role']}" for u in registry.used_by("skills", reg_id)) or "-"
            rows.append((reg_id, entry.get("name") or reg_id, entry["path"], users,
                         entry.get("description") or "", "、".join(entry.get("sources") or []) or "-"))
        table(rows)
        return 0
    if action == "import":
        groups = registry.skill_candidates()
        if not a.all and not a.names:
            if not groups:
                print("（没有在各级技能目录里找到候选）")
                return 0
            for group in groups:
                mark = "  ← 同名不同内容" if group["conflict"] else ""
                print(f"{group['name']}{mark}")
                rows = [("登记 ID", "目录（同内容合并）", "来源", "已登记")]
                for v in group["variants"]:
                    rows.append((v["id"], "、".join(v["paths"]), "、".join(v["sources"]),
                                 "是" if v["registered"] else "-"))
                table(rows)
                print()
            print("导入：agent-dispatch skills import --all 或 import <登记ID | 技能名> [--source <来源>]；"
                  "同名不同内容的要按登记 ID 或来源指明")
            return 0
        if a.all:
            selections = [v["id"] for group in groups for v in group["variants"] if not v["registered"]]
        else:
            selections = [{"name": n, "source": a.source} for n in a.names]
        added = registry.import_skills(selections)
        store.info(f"新登记 {len(added)} 个技能" + (f"：{'、'.join(added)}" if added else "（没有新的）"))
        return 0
    if not a.names:
        raise DispatchError(f"skills {action} 需要登记 ID")
    if action == "add":
        if len(a.names) < 2:
            raise DispatchError("用法：agent-dispatch skills add <登记ID> <技能目录> [--name 技能名]")
        registry.save_skill(a.names[0], a.names[1], a.name)
    elif action == "remove":
        registry.remove_skill(a.names[0])
    store.info(f"已处理：skills {action} {a.names[0]}")
    return 0


def cmd_roles(a):
    action = a.action or "list"
    project = None if a.is_global else current_project(a)
    if action == "list":
        print(f"项目 {project['name']}（{project['id']}）可派的角色：\n" if project else "全局角色（不在任何项目里；看某个项目的用 --project）：\n")
        table(role_table(project))
        return 0
    if not a.name:
        raise DispatchError(f"roles {action} 需要角色 ID")
    if action == "show":
        role = definitions._merged_role(project, definitions.find_role_id(project, a.name))
        public = {k: v for k, v in role.items() if not k.startswith("_")}
        print(json.dumps(public, ensure_ascii=False, indent=2))
        if "mcp" in role or "skills" in role:
            print()
            for line in inject.role_notes():
                print(line)
        print(f"\n派工单模板：{role['_brief_path'] or '（无，任务原文直接作为派工单）'}")
        if role["_brief_path"]:
            print("\n" + (store.read_text(role["_brief_path"]) or ""))
        return 0
    if action == "pool":
        path = definitions.set_pool(project, a.name, None if a.clear else parse_pool(a.pool))
        if project is None:
            store.info("这是全局角色的模型池：各项目里没有自己配置模型池的同名角色都会用它")
        store.info(f"已{'去掉' if a.clear else '更新'}角色 {a.name} 的模型池：{path}")
        return 0
    if action == "add":
        path, inherits = definitions.add_role(a.name, project, a.title, a.description,
                                              [r.strip() for r in a.repos.split(",")] if a.repos else None, a.backend, a.mode)
        where = f"项目 {project['id']}" if project else "全局"
        store.info(f"已在{where}新建角色 {a.name}：{path}" + ("（覆盖全局同名角色，只需写要改的项）" if inherits else ""))
        if not inherits:
            store.info(f"派工单模板：{path[:-5]}.brief.md —— 把这个角色的职责、边界写进去")
        return 0
    raise DispatchError(f"未知动作 {action}")


WEB_STATE = os.path.join(store.HOME, "web.json")


def web_running():
    """后台网页服务的登记信息；进程已不在则视为没在跑。"""
    try:
        info = json.loads(store.read_text(WEB_STATE) or "")
    except ValueError:
        return None
    return info if store.alive(info.get("pid")) else None


def cmd_web(a):
    from . import web
    worker = os.environ.get("AGENT_DISPATCH_WORKER")
    if worker and a.action in ("start", "stop") and not os.environ.get("AGENT_DISPATCH_HOME"):
        # 执行者自测时忘了指定临时状态目录，就会停掉或占用用户自己的网页服务
        raise DispatchError(f"你是作业 {worker} 的执行者，不能启停用户的网页服务。自测请设置 AGENT_DISPATCH_HOME 指向临时目录并换端口")
    running = web_running()
    if a.action == "status":
        print(f"后台网页在运行：{running['url']}  (pid {running['pid']})" if running else "后台网页没有在运行")
        return 0
    if a.action == "stop":
        if not running:
            store.info("后台网页没有在运行")
            return 0
        os.kill(running["pid"], signal.SIGTERM)
        os.remove(WEB_STATE)
        store.info(f"已停止后台网页（pid {running['pid']}）。不影响任何作业")
        return 0
    if a.action == "start":
        if running:
            store.info(f"后台网页已经在运行：{running['url']}")
        else:
            env = dict(os.environ, PYTHONPATH=backends.ROOT + os.pathsep + os.environ.get("PYTHONPATH", ""))
            os.makedirs(store.HOME, exist_ok=True)
            with open(os.path.join(store.HOME, "web.log"), "a", encoding="utf-8") as log:
                proc = subprocess.Popen([sys.executable, "-m", "agent_dispatch", "web", "--host", a.host, "--port", str(a.port)],
                                        env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
            time.sleep(0.8)
            if proc.poll() is not None:
                raise DispatchError(f"后台网页没启动成功（端口 {a.port} 可能被占用）。详见 {os.path.join(store.HOME, 'web.log')}")
            running = {"pid": proc.pid, "url": f"http://{a.host}:{a.port}/"}
            store.write_text(WEB_STATE, json.dumps(running))
            store.info(f"后台网页已启动：{running['url']}    停止：agent-dispatch web stop")
        if a.open:
            webbrowser.open(running["url"])
        return 0
    if a.open:
        webbrowser.open(f"http://{a.host}:{a.port}/")
    return web.serve(a.host, a.port)


def cmd_run(a):
    runner.run(a.job)
    return 0


def build_parser():
    ap = argparse.ArgumentParser(prog="agent-dispatch", description="纯命令行的多智能体派工")
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="命令")

    def add(name, func, help_text):
        parser = sub.add_parser(name, help=help_text, description=help_text)
        parser.set_defaults(func=func)
        return parser

    p = add("start", cmd_start, "派出一个新作业，立即返回")
    p.add_argument("job", help="作业名（字母数字 . _ -）")
    p.add_argument("task", nargs="*", help="任务文本；'-' 表示从标准输入读")
    p.add_argument("--project", "-P", help="项目 ID 或名称（在项目的工程目录里执行时可省略，自动识别）")
    p.add_argument("--role", "-r", help="角色 ID 或称呼，如 tester / 测试工程师（见 agent-dispatch roles）")
    p.add_argument("--repo", help="工程 ID：角色负责多个工程时用它指明")
    p.add_argument("--profile", "-p", help="早先的 profile 名（已被 --project/--role 取代）")
    p.add_argument("--backend", "-b", help="后端：pi / opencode …（默认取角色定义）")
    p.add_argument("--model", "-m", help="模型（默认取角色定义或后端默认）")
    p.add_argument("--batch", help="批次名：同一次请求派出的作业用同一个批次（即一个事项）")
    p.add_argument("--batch-title", dest="batch_title", help="事项标题：这个批次是因为要办什么事")
    p.add_argument("--batch-reason", dest="batch_reason", help="事项原因：为什么派这批单")
    p.add_argument("--follows", action="append", default=[], metavar="作业名",
                   help="这张单是因为那张单才派的（重派、按复核意见修复、接着上一步），可重复")
    p.add_argument("--channels", help="浏览器通道：逗号分隔，或 all / none（默认取 profile）")
    p.add_argument("--cwd", help="工作目录（默认取 profile）")
    p.add_argument("--file", "-f", help="从文件读任务文本")
    p.add_argument("--mode", choices=["headless", "tmux"],
                   help="运行方式：headless 无头；tmux 交互界面跑在 tmux 里，可接入、两轮之间不退出（默认取 profile）")
    p.add_argument("--timeout", type=float, help="单轮最长分钟数，超时终止（默认取 profile）")
    p.add_argument("--session-title", dest="session_title", help="一句话说明这次会话在办什么事，总览里可按会话查看")
    p.add_argument("--dry-run", action="store_true", help="只打印派工单，不派出")
    p.add_argument("--json", action="store_true", help="配合 --dry-run：输出结构化的预览 JSON")

    p = add("sessions", cmd_sessions, "按派工会话汇总：派了几张单、各是什么状态")
    p.add_argument("--project", "-P", help="只看这个项目的会话（项目 ID）")

    p = add("batches", cmd_batches, "按事项汇总：每个批次一行，标题、原因、谁派的、各状态数量")
    p.add_argument("--project", "-P", help="只看这个项目的事项（项目 ID）")

    p = add("batch", cmd_batch, "查看一个事项（批次）；事后补 --title / --reason")
    p.add_argument("batch", help="批次名")
    p.add_argument("--title", help="补上或修改事项标题")
    p.add_argument("--reason", help="补上或修改事项原因")

    p = add("wait", cmd_wait, "限时等待作业落定；超时返回码 3，只表示还在跑")
    p.add_argument("jobs", nargs="*")
    p.add_argument("--batch", help="等这个批次的全部作业")
    p.add_argument("--timeout", type=int, default=540, help="最多等多少秒（默认 540）")

    p = add("status", cmd_status, "作业总览，或单个作业的详情")
    p.add_argument("job", nargs="?")
    p.add_argument("--batch", help="只看这个批次")
    p.add_argument("--project", dest="group", help="只看这个项目（按项目名）")
    p.add_argument("--repo", dest="project", help="只看这个工程（按工程名）")
    p.add_argument("--watch", "-w", action="store_true", help="每 3 秒刷新")
    p.add_argument("--json", action="store_true", help="输出台账 JSON")

    p = add("log", cmd_log, "看执行者的输出")
    p.add_argument("job")
    p.add_argument("-n", type=int, default=60, help="最后多少行（默认 60）")
    p.add_argument("--follow", "-f", action="store_true", help="持续跟随，作业结束后自动退出")

    p = add("history", cmd_history, "逐步查看工具调用与回复")
    p.add_argument("job")
    p.add_argument("-n", type=int, default=30, help="最后多少条（默认 30）")
    p.add_argument("--full", action="store_true", help="不截断")

    p = add("reply", cmd_reply, "在同一会话里续派（裁决、补充说明、返工）")
    p.add_argument("job")
    p.add_argument("task", nargs="*", help="内容；'-' 表示从标准输入读")
    p.add_argument("--file", "-f")
    p.add_argument("--force", action="store_true", help="对已验收的作业继续派")

    p = add("follow", cmd_follow, "tmux 界面还在跑但作业已落定时，重新跟进它（不发送任何内容）")
    p.add_argument("job")
    p.add_argument("--note")

    p = add("stop", cmd_stop, "叫停运行中的作业并释放通道")
    p.add_argument("job")
    p.add_argument("--reason", help="叫停原因，记入裁决记录")

    p = add("attach", cmd_attach, "接进 tmux 方式作业的界面：可以看，也可以直接打字（Ctrl-b d 退出，不影响作业）")
    p.add_argument("job")

    p = add("take", cmd_take, "人工接手：主管不再往里派工，通道归你")
    p.add_argument("job")
    p.add_argument("--stop", action="store_true", help="作业在运行时先叫停")
    p.add_argument("--note")

    p = add("release", cmd_release, "把人工持有的作业交还主管")
    p.add_argument("job")
    p.add_argument("--note")

    p = add("gate", cmd_gate, "跑机械闸门；有硬性项未通过时返回码 1")
    p.add_argument("job")

    p = add("accept", cmd_accept, "验收")
    p.add_argument("job")
    p.add_argument("--note")
    p.add_argument("--force", action="store_true", help="闸门未过或状态不符时强制验收，必须配 --note")

    p = add("reject", cmd_reject, "驳回（不再续派）")
    p.add_argument("job")
    p.add_argument("--reason", required=True)

    p = add("escalate", cmd_escalate, "标记「等待用户决定」，在总览里置顶")
    p.add_argument("job")
    p.add_argument("question", nargs="*")
    p.add_argument("--clear", action="store_true", help="清除标记")

    p = add("role", cmd_role, "打印主管角色文档（职责、工作循环、裁决表）；在项目里会附上项目规则和可派的角色")
    p.add_argument("--project", "-P", help="项目 ID 或名称（默认按当前目录识别）")

    p = add("backends", cmd_backends, "派工工具（pi、opencode……）是否可用：列表 / enable <名> / disable <名>")
    p.add_argument("action", nargs="?", choices=["list", "enable", "disable"])
    p.add_argument("name", nargs="?")

    p = add("models", cmd_models, "模型登记：列表 / import / limit / add / enable / disable / note / resume / remove")
    p.add_argument("action", nargs="?", choices=["list", "import", "limit", "add", "enable", "disable", "note", "resume", "remove"])
    p.add_argument("alias", nargs="?", help="模型短名，如 pi:glm-5.3-flash")
    p.add_argument("value", nargs="?", help="limit：N 或 default（恢复用全局默认值）")
    p.add_argument("names", nargs="*", help="import：要导入的模型 ID 列表")
    p.add_argument("--backend", help="add：后端；import：只看 / 只导入这个后端的模型（不给则全部）")
    p.add_argument("--id", help="add：后端里的模型 ID，如 cc-switch-zhipu-glm/glm-5.3-flash")
    p.add_argument("--note", help="add / note：备注")
    p.add_argument("--list", dest="list_candidates", action="store_true", help="import：先列出工具里的候选模型及登记情况，不导入")
    p.add_argument("--default", dest="default_limit", type=int, metavar="N", help="limit：设置全局默认并发上限")

    p = add("projects", cmd_projects, "项目定义：列表 / show <ID> / add <ID> / add-repo <ID> <工程名>=<目录>")
    p.add_argument("action", nargs="?", choices=["list", "show", "add", "add-repo"])
    p.add_argument("args", nargs="*")
    p.add_argument("--name", help="add：项目显示名")
    p.add_argument("--repo", action="append", metavar="工程名=目录", help="add：登记一个工程，可重复")
    p.add_argument("--channels", help="add-repo：这个工程的浏览器通道，逗号分隔")

    p = add("mcp", cmd_mcp, "MCP 服务登记：list / import [--all | 登记ID…] / add <登记ID> / remove <登记ID>；角色的 mcp 字段引用登记 ID")
    p.add_argument("action", nargs="?", choices=["list", "import", "add", "remove"])
    p.add_argument("names", nargs="*")
    p.add_argument("--all", action="store_true", help="import：导入全部候选（同名不同内容的各成一条）")
    p.add_argument("--source", help="import：同名候选出现多处时指明来源")
    p.add_argument("--type", choices=["local", "remote"], help="add：类型（给了 url 默认 remote，否则 local）")
    p.add_argument("--command", help="add：local 的可执行命令")
    p.add_argument("--args", action="append", help="add：参数，逗号分隔，可重复")
    p.add_argument("--env", action="append", metavar="键=值", help="add：环境变量，可重复；只存本机，输出时遮罩")
    p.add_argument("--headers", action="append", metavar="键=值", help="add：remote 的请求头，可重复；只存本机，输出时遮罩")
    p.add_argument("--url", help="add：remote 的地址")
    p.add_argument("--description", help="add：一句话说明")
    p.add_argument("--name", help="add：下发给执行者的服务名（缺省等于登记 ID）")

    p = add("skills", cmd_skills, "技能登记：list / import [--all | 登记ID…] / add <登记ID> <目录> / remove <登记ID>；角色的 skills 字段引用登记 ID")
    p.add_argument("action", nargs="?", choices=["list", "import", "add", "remove"])
    p.add_argument("names", nargs="*")
    p.add_argument("--all", action="store_true", help="import：导入全部候选（同名不同内容的各成一条）")
    p.add_argument("--source", help="import：同名候选出现多处时指明来源")
    p.add_argument("--name", help="add：技能名（缺省取目录名）")

    p = add("roles", cmd_roles, "角色定义：列表 / show <角色> / add <角色ID> / pool <角色> <模型短名>=<比例> …")
    p.add_argument("action", nargs="?", choices=["list", "show", "add", "pool"])
    p.add_argument("name", nargs="?")
    p.add_argument("pool", nargs="*", help="pool：模型池，如 pi:glm-5.3-flash=70 oc:glm-5=30")
    p.add_argument("--clear", action="store_true", help="pool：去掉模型池")
    p.add_argument("--project", "-P", help="项目 ID 或名称（默认按当前目录识别）")
    p.add_argument("--global", dest="is_global", action="store_true", help="只看 / 新建全局角色，或配置全局角色的模型池")
    p.add_argument("--title", help="add：角色的称呼，如 后端开发工程师")
    p.add_argument("--description", help="add：一句话说明")
    p.add_argument("--repos", help="add：这个角色负责的工程 ID，逗号分隔")
    p.add_argument("--backend", help="add：默认后端")
    p.add_argument("--mode", choices=["headless", "tmux"], help="add：默认运行方式")

    p = add("web", cmd_web, "本地网页总览与配置：web start 后台运行，web stop 停止；不带动作则在前台运行")
    p.add_argument("action", nargs="?", choices=["start", "stop", "status"], help="start 后台启动 / stop 停止 / status 查看；省略则前台运行")
    p.add_argument("--open", action="store_true", help="同时在浏览器里打开")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)

    p = sub.add_parser("_run")  # 内部：后台运行器入口
    p.add_argument("job")
    p.set_defaults(func=cmd_run)
    return ap


# 可以接收多个位置参数的字段：Python 3.11 及以前的 argparse 不接受「选项之后再跟这些位置参数」，要自己收回来
TRAILING_LISTS = ("task", "question", "pool", "args", "jobs", "names")


def main(argv=None):
    parser = build_parser()
    args, extra = parser.parse_known_args(argv)
    if extra:
        target = next((name for name in TRAILING_LISTS if isinstance(getattr(args, name, None), list)), None)
        if target is None or any(token.startswith("-") and token != "-" for token in extra):
            parser.error(f"无法识别的参数：{' '.join(extra)}")
        setattr(args, target, getattr(args, target) + extra)
    try:
        return args.func(args) or 0
    except DispatchError as exc:
        print(f"[agent-dispatch] 错误：{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
