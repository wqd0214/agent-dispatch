"""本地网页总览：新界面（ui/ 构建产物）只读作业 + 配置中心（项目 / 角色 / 模型登记表）。关掉不影响派工。"""
import hashlib
import json
import mimetypes
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import backends, cli, definitions, gates, inject, models, registry, store, tmux

UI_DIST = os.environ.get("AGENT_DISPATCH_UI_DIST") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui_dist")
TEXT_SUFFIXES = (".md", ".json", ".jsonl", ".txt", ".log", ".sql", ".csv")


PKG_DIR = os.path.dirname(os.path.abspath(__file__))
VERSION_TTL = 3.0  # 磁盘版本的缓存秒数：总览轮询很频繁，不能每次都 stat 全部源文件
_version_cache = (0.0, "")


def code_version(directory=None):
    """对目录下 *.py 的名字、大小、修改时间算短摘要；不读内容，开销极小。"""
    global _version_cache
    directory = directory or PKG_DIR
    now = time.time()
    if directory == PKG_DIR and _version_cache[1] and now - _version_cache[0] < VERSION_TTL:
        return _version_cache[1]
    parts = []
    for name in sorted(os.listdir(directory)):
        if name.endswith(".py"):
            st = os.stat(os.path.join(directory, name))
            parts.append(f"{name}:{st.st_size}:{st.st_mtime_ns}")
    digest = hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()[:10]
    if directory == PKG_DIR:
        _version_cache = (now, digest)
    return digest


PROCESS_VERSION = code_version()  # 进程启动时的代码版本；之后磁盘再变它也不变


def version_info():
    """进程启动时的版本 vs 磁盘现在的版本：不一致说明服务还在跑旧代码，需要重启。"""
    return {"process": PROCESS_VERSION, "disk": code_version()}


def stale_hint(url):
    """web status 用：问运行中的服务要两个版本；拿不到（老进程没这接口）或对不上都提示重启。"""
    hint = "运行中的服务是旧版本，需要重启：agent-dispatch web stop && agent-dispatch web start"
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/api/version", timeout=3) as response:
            info = json.load(response)
    except (OSError, ValueError):
        return hint
    return None if info.get("process") == info.get("disk") else hint


def ui_dist_dir():
    """前端构建产物目录（可用 AGENT_DISPATCH_UI_DIST 覆盖）；没有 index.html 视为不存在。"""
    directory = os.path.expandvars(UI_DIST)
    return directory if os.path.isfile(os.path.join(directory, "index.html")) else None


def profile_of(job):
    try:
        return cli.load_profile(job.get("profile"))
    except store.DispatchError:
        return {"name": job.get("profile")}


def run_dir_of(job, profile):
    if job.get("run_dir") and os.path.isdir(job["run_dir"]):
        return job["run_dir"]
    if store.effective_state(job) == "running":
        return None
    return gates.resolve_run_dir(job, profile)


def last_log_line(name):
    path = store.job_path(name, "run.log")
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 4000))
            lines = f.read().decode("utf-8", errors="replace").strip().splitlines()
    except OSError:
        return ""
    return lines[-1] if lines else ""


def summarize(job, batch_titles=None, session_titles=None, batch_reasons=None):
    state = store.effective_state(job)
    start = job.get("turn_started_ts")
    end = time.time() if state in ("running", "queued") else job.get("ended_ts") or start
    group, project = store.project_of(job)
    escalated = job.get("needs_user")
    dispatcher = job.get("dispatcher") or None
    if dispatcher and not dispatcher.get("title"):
        dispatcher = dict(dispatcher, title=(session_titles or {}).get(dispatcher.get("session_id")) or None)
    return {
        "name": job["name"], "batch": job.get("batch") or "", "profile": job.get("profile"),
        "batch_title": (batch_titles or {}).get(job.get("batch")) or "",
        "batch_reason": (batch_reasons or {}).get(job.get("batch")) or "",
        "group": group, "project": project, "role": job.get("role"), "role_title": job.get("role_title"),
        "backend": job.get("backend"), "model": job.get("model"), "model_alias": job.get("model_alias"),
        "model_actual": job.get("model_actual"), "state": state,
        "state_label": cli.STATE_LABEL.get(state, state), "status": job.get("status"), "turn": job.get("turn"),
        "seconds": int(max(0, end - start)) if start else None, "channels": job.get("channels") or [],
        "dispatcher": dispatcher, "follows": job.get("follows") or [],
        "task": job.get("task", ""), "needs_user": escalated.get("question") if isinstance(escalated, dict) else escalated, "reason": job.get("reason"),
        "gate_ok": (job.get("gate") or {}).get("ok"), "created": job.get("created"), "mode": job.get("mode", "headless"),
        "updated": job.get("updated"), "ended": job.get("ended"), "waiting_models": job.get("waiting_models") or [],
        "activity": last_log_line(job["name"]) if state == "running" else "",
    }


def defined_projects():
    """已定义的项目、工程和角色：让还没有作业的工程和角色也能在页面上看到。"""
    out = []
    for project in definitions.list_projects():
        roles = [{"id": r["role"], "title": r["title"], "description": r.get("description") or "", "repos": r["repos"],
                  "backend": r.get("backend"), "mode": r.get("mode") or "headless", "scope": r["scope"]}
                 for r in definitions.list_roles(project)]
        out.append({"id": project["id"], "name": project["name"], "roles": roles,
                    "repos": [{"id": repo["id"], "name": repo["name"], "cwd": repo["cwd"], "channels": repo["channels"],
                               "channel_ns": repo["channel_ns"]} for repo in project["repos"].values()]})
    return out


def overview():
    jobs = store.all_jobs()
    definitions_ = defined_projects()
    # 事项标题、会话标题：批次数不多，一次在内存里建好映射避免逐个读文件
    batch_titles, batch_reasons, session_titles = {}, {}, {}
    for job in jobs:
        if job.get("batch") and job["batch"] not in batch_titles:
            meta = store.load_batch(job["batch"]) or {}
            batch_titles[job["batch"]] = meta.get("title") or ""
            batch_reasons[job["batch"]] = meta.get("reason") or ""
        sid = (job.get("dispatcher") or {}).get("session_id")
        if sid and sid not in session_titles:
            session_titles[sid] = (store.load_session(sid) or {}).get("title") or ""
    profiles, channels, cases = {}, {}, []
    for project in definitions_:  # 定义里带通道的工程：没有作业也显示通道
        for repo in project["repos"]:
            if repo["channels"]:
                channels[repo["channel_ns"]] = {"group": project["name"], "project": repo["name"], "profile": repo["channel_ns"],
                                                "table": store.channel_table(repo["channel_ns"], repo["channels"])}
    for job in jobs:
        group, project = store.project_of(job)
        profile = profiles.setdefault(job.get("profile"), profile_of(job))
        namespace = store.channel_ns(job)
        if profile.get("channels") and namespace not in channels:
            channels[namespace] = {"group": group, "project": project, "profile": namespace,
                                   "table": store.channel_table(namespace, profile["channels"])}
        run_dir = run_dir_of(job, profile)
        for verdict in gates.load_verdicts(run_dir) if run_dir else []:
            cases.append({"tc": verdict.get("tc"), "verdict": verdict.get("verdict"), "evidence": verdict.get("evidence"),
                          "note": verdict.get("note"), "job": job["name"], "batch": job.get("batch") or "",
                          "group": group, "project": project, "run": os.path.basename(run_dir)})
    return {"now": store.now(), "home": store.HOME, "versions": version_info(),
            "jobs": [summarize(j, batch_titles, session_titles, batch_reasons) for j in jobs], "cases": cases,
            "channels": list(channels.values()), "definitions": definitions_}


def model_page():
    """模型登记表、各模型的使用统计、各角色的模型池与实际配比。"""
    registry, paused, jobs = models.load_registry(), models.health(), store.all_jobs()
    by_id = {(m["backend"], m["id"]): alias for alias, m in registry["models"].items()}
    stats, provider_failures = {}, {}
    for job in jobs:
        for attempt in job.get("model_attempts") or []:
            provider_failures[attempt["model"]] = provider_failures.get(attempt["model"], 0) + 1
        alias = job.get("model_alias") or by_id.get((job.get("backend"), job.get("model")))
        if not alias:
            continue
        item = stats.setdefault(alias, {"jobs": 0, "pass": 0, "settled": 0, "failed": 0, "seconds": 0})
        item["jobs"] += 1
        state = store.effective_state(job)
        if state != "running" and job.get("created_ts") and job.get("ended_ts"):
            item["settled"] += 1
            item["seconds"] += job["ended_ts"] - job["created_ts"]
            item["pass"] += job.get("status") == "PASS"
            item["failed"] += state in ("failed", "interrupted")
    used = {}
    role_titles = {(r["role"], None): r["title"] for r in definitions.list_roles(None)}
    project_names = {"": "全局"}
    for project in definitions.list_projects():
        project_names[project["id"]] = project["name"]
        for r in definitions.list_roles(project):
            role_titles[(r["role"], project["id"])] = r["title"]
    for project_id, role_id, pool in models.all_pools():
        for entry in pool:
            used.setdefault(entry["model"], []).append({
                "project": project_id, "project_name": project_names.get(project_id, project_id or "全局"),
                "role": role_id, "role_title": role_titles.get((role_id, project_id)) or role_titles.get((role_id, None)) or role_id,
                "weight": entry["weight"]})
    model_rows = []
    counts = models.occupancy()
    discovered = {(b, mid) for b, mid in models.discover()}
    discover_backends = {b for b, _ in discovered}
    for alias, model in registry["models"].items():
        st = stats.get(alias, {"jobs": 0, "pass": 0, "settled": 0, "failed": 0, "seconds": 0})
        key = (model["backend"], model["id"])
        model_rows.append({"alias": alias, "backend": model["backend"], "id": model["id"], "enabled": model.get("enabled", True),
                           "note": model.get("note", ""), "paused": paused.get(alias), "used_by": used.get(alias, []),
                           "jobs": st["jobs"], "settled": st["settled"], "pass": st["pass"], "failed": st["failed"],
                           "avg_seconds": int(st["seconds"] / st["settled"]) if st["settled"] else None,
                           "provider_failures": provider_failures.get(alias, 0),
                           "running": counts.get(key, 0),
                           "limit": model.get("max_concurrent") or registry.get("default_max_concurrent", 5),
                           "limit_source": "model" if model.get("max_concurrent") else "default",
                           # 只在工具还能列出模型时才标缺失，避免把没装的工具里的模型误标
                           "missing": model["backend"] in discover_backends and key not in discovered})
    model_rows.sort(key=lambda m: (not m["used_by"], not m["enabled"], m["backend"], m["alias"]))
    def role_row(project, role, counts):
        return {"project": project["id"] if project else "", "project_name": project["name"] if project else "全局角色",
                "role": role["role"], "title": role["title"], "repos": role["repos"], "pool": role.get("model_pool") or [],
                "pool_source": role.get("pool_source"), "usage": counts,
                "fallback": role.get("model") or (role.get("models") or {}).get(role.get("backend") or "")
                or f"{role.get('backend') or '-'} 的默认模型"}

    roles = [role_row(None, role, models.usage("", role["role"], any_project=True)) for role in definitions.list_roles(None)]
    for project in definitions.list_projects():
        roles += [role_row(project, role, models.usage(project["id"], role["role"])) for role in definitions.list_roles(project)]
    tools = [{"name": name, "enabled": backends.enabled(name), "default_model": backend.get("default_model"),
              "models": sum(1 for m in registry["models"].values() if m["backend"] == name)}
             for name, backend in sorted(backends.load_all().items())]
    for row in model_rows:
        row["backend_enabled"] = backends.enabled(row["backend"])
    return {"cooldown_min": registry["cooldown_min"], "default_max_concurrent": registry.get("default_max_concurrent", 5),
            "models": model_rows, "roles": roles,
            "backends": [t["name"] for t in tools], "tools": tools}


def config_overview():
    """配置中心总览：全部项目（含工程、默认角色、defaults、主管规则）和全局角色列表。"""
    projects = []
    for project in definitions.list_projects():
        projects.append({
            "id": project["id"], "name": project["name"], "default_role": project.get("default_role"),
            "defaults": project.get("defaults") or {}, "supervisor": definitions.get_supervisor_notes(project["id"]),
            "repos": [{"id": repo["id"], "name": repo["name"], "cwd": repo["cwd"], "channels": repo["channels"],
                       "channel_ns": repo["channel_ns"]} for repo in project["repos"].values()],
            "roles": [{"id": r["role"], "title": r["title"], "description": r.get("description") or "",
                       "repos": r["repos"], "scope": r["scope"]} for r in definitions.list_roles(project)],
        })
    roles = [{"id": r["role"], "title": r["title"], "description": r.get("description") or ""}
             for r in definitions.list_roles(None)]
    return {"projects": projects, "roles": roles}


def config_project(data):
    """请求体里的 project 为空表示全局角色。"""
    return definitions.load_project(data["project"]) if data.get("project") else None


def dispatch_options():
    """派工表单的选项：项目、工程和可派的角色。"""
    out = []
    for project in definitions.list_projects():
        roles = [{"id": r["role"], "title": r["title"], "description": r.get("description") or "",
                  "repos": r["repos"], "pooled": bool(r.get("model_pool"))} for r in definitions.list_roles(project)]
        out.append({"id": project["id"], "name": project["name"],
                    "repos": [{"id": repo_id, "name": repo["name"]} for repo_id, repo in project["repos"].items()],
                    "roles": roles})
    return {"projects": out}


def run_cli(args, stdin_text=None):
    """用子进程调命令行，复用其中的业务规则。返回 (返回码, 提示文字, 错误文字, 标准输出)。"""
    env = dict(os.environ, PYTHONPATH=backends.ROOT)
    try:
        kwargs = {"input": stdin_text} if stdin_text is not None else {"stdin": subprocess.DEVNULL}
        proc = subprocess.run([sys.executable, "-m", "agent_dispatch", *args], env=env,
                              capture_output=True, text=True, timeout=60, **kwargs)
    except subprocess.TimeoutExpired:
        return None, "", "命令超时（60 秒）", ""
    text = (proc.stderr + proc.stdout).strip()
    error = ""
    if proc.returncode != 0:
        marker = "[agent-dispatch] 错误："
        lines = [line[len(marker):].strip() for line in proc.stderr.splitlines() if line.startswith(marker)]
        error = lines[-1] if lines else (text or f"命令失败（返回码 {proc.returncode}）")
    return proc.returncode, text, error, proc.stdout.strip()


def dispatch_start_args(data):
    """把派工表单的请求体变成 (作业名, 任务文本, start 的参数列表)。任务文本走标准输入，避免被当成选项。"""
    role = (data.get("role") or "").strip()
    if not role:
        raise store.DispatchError("缺少角色")
    job = (data.get("job") or "").strip() or f"{role}-{time.strftime('%m%d-%H%M%S')}"
    store.check_name(job)
    task = (data.get("task") or "").strip()
    if not task:
        raise store.DispatchError("任务为空")
    args = ["start", job]
    if data.get("project"):
        args += ["--project", data["project"]]
    args += ["--role", role]
    if data.get("repo"):
        args += ["--repo", data["repo"]]
    if data.get("batch"):
        args += ["--batch", data["batch"]]
        if data.get("batch_title"):
            args += ["--batch-title", data["batch_title"]]
        if data.get("batch_reason"):
            args += ["--batch-reason", data["batch_reason"]]
    for followed in data.get("follows") or []:
        args += ["--follows", followed]
    args += ["-"]
    return job, task, args


def list_run_files(run_dir):
    files = []
    for base, dirs, names in os.walk(run_dir):
        dirs.sort()
        for filename in sorted(names):
            path = os.path.join(base, filename)
            files.append({"path": os.path.relpath(path, run_dir), "size": os.path.getsize(path)})
            if len(files) >= 400:
                return files
    return files


def job_detail(name):
    job = store.load(name)
    profile = profile_of(job)
    run_dir = run_dir_of(job, profile)
    try:
        attach = cli.how_to_attach(job)
    except store.DispatchError:
        attach = None
    meta = store.load_batch(job.get("batch")) or {}
    batch_titles = {job["batch"]: meta.get("title") or ""} if job.get("batch") else {}
    batch_reasons = {job["batch"]: meta.get("reason") or ""} if job.get("batch") else {}
    detail = summarize(job, batch_titles, {}, batch_reasons)
    detail.update({
        "followed_by": [j["name"] for j in store.all_jobs() if job["name"] in (j.get("follows") or [])],
        "cwd": job.get("cwd"), "session_id": job.get("session_id"), "attach": attach,
        "mode": job.get("mode", "headless"), "tmux_alive": tmux.alive(job.get("tmux")),
        "inject": inject.describe_job(job),
        "report": job.get("report") or {}, "gate": job.get("gate"), "decisions": job.get("decisions") or [],
        "turns": job.get("turns") or [], "brief": store.read_text(store.job_path(name, "brief.md"), ""),
        "result": store.read_text(store.job_path(name, "result.md"), ""), "events": cli.read_events(name)[-500:],
        "run_dir": run_dir, "run_files": list_run_files(run_dir) if run_dir else [],
        "job_dir": store.job_dir(name),
    })
    return detail


def read_log(name, offset):
    path = store.job_path(name, "run.log")
    try:
        size = os.path.getsize(path)
        if offset < 0 or offset > size:
            offset = max(0, size - 200_000)
        with open(path, "rb") as f:
            f.seek(offset)
            chunk = f.read(2_000_000)
    except OSError:
        return {"offset": 0, "text": ""}
    return {"offset": offset + len(chunk), "text": chunk.decode("utf-8", errors="replace")}


def registry_page(kind, project_query):
    """MCP / 技能页的数据：project_query 为空是全局视图（只看全局登记），
    否则该项目的 + 全局的，并带本项目是否管理、启用清单、各角色有没有单独覆盖。"""
    project = definitions.load_project(project_query) if project_query else None
    reg = registry.load_mcp() if kind == "mcp" else registry.load_skills()
    scope = project["id"] if project else ""
    entries = []
    for reg_id, entry in sorted(registry.entries_for(reg, scope).items()):
        item = {"id": reg_id, "name": entry.get("name") or reg_id, "project": registry.entry_project(entry),
                "sources": entry.get("sources") or [], "used_by": registry.used_by(kind, reg_id)}
        if kind == "mcp":
            item.update(registry.mask_entry(entry))
        else:
            item.update(entry)
        entries.append(item)
    defaults = (project.get("defaults") or {}) if project else {}
    roles = []
    for role in definitions.list_roles(project):
        field = definitions.role_detail(project, role["role"])["fields"].get(kind)
        # 角色层自己写了这个字段 = 单独配置；否则跟随项目默认（或全局层）
        own_layer = "项目" if project else "本机全局"
        roles.append({"id": role["role"], "title": role["title"], "managed": field is not None,
                      "value": field["value"] if field else None, "override": bool(field) and field["source"] == own_layer,
                      "source": field["source"] if field else None})
    return {"entries": entries, "managed": kind in defaults, "enabled": defaults.get(kind) or [], "roles": roles}


def registry_candidates(kind, project_query):
    """候选拉取：project_query 为 None 时看全部来源（兼容旧调用），空串只看全局，否则该项目 + 全局。"""
    groups = (registry.mcp_candidates if kind == "mcp" else registry.skill_candidates)(project=project_query)
    if kind == "mcp":
        return [dict(group, variants=[dict(v, entry=registry.mask_entry(v["entry"]),
                                            diff={k: registry.mask_any(x) for k, x in v["diff"].items()})
                                      for v in group["variants"]])
                for group in groups]
    return groups


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        if os.environ.get("AGENT_DISPATCH_ACCESS_LOG"):  # 排障用：记下每个请求和发起方
            sys.stderr.write(f"[{store.now()}] {self.command} {self.path} {args[1] if len(args) > 1 else ''} "
                             f"UA={self.headers.get('User-Agent', '-')[:60]}\n")
            sys.stderr.flush()

    def send_body(self, body, content_type, status=200, cache=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache or "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, data, status=200):
        self.send_body(json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", status)

    def send_page(self, path, cache=None):
        with open(path, "rb") as f:
            return self.send_body(f.read(), "text/html; charset=utf-8", cache=cache)

    def send_ui_file(self, relative):
        """送 ui_dist 里的静态文件；解析后的真实路径必须在 ui_dist 之内。"""
        base = os.path.realpath(ui_dist_dir())
        path = os.path.realpath(os.path.join(base, relative))
        if not path.startswith(base + os.sep) or not os.path.isfile(path):
            return self.send_json({"error": "not found"}, 404)
        content_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if path.endswith(".js"):
            content_type = "text/javascript"
        if path.endswith(".html"):
            content_type = "text/html; charset=utf-8"
        # assets/ 下是带哈希的文件名，内容不变，可以长缓存；其余不缓存
        cache = "public, max-age=31536000, immutable" if relative.startswith("assets/") else None
        with open(path, "rb") as f:
            return self.send_body(f.read(), content_type, cache=cache)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(url.query, keep_blank_values=True)  # project= 空串表示「只要全局来源」
        parts = [urllib.parse.unquote(p) for p in url.path.split("/") if p]
        ui = ui_dist_dir()
        try:
            if not parts:
                if ui:
                    return self.send_page(os.path.join(ui, "index.html"))
                return self.send_json({"error": "前端界面未构建：在 ui/ 下执行 pnpm build 后重试"}, 404)
            if parts[0] != "api" and ui:
                if any(p == ".." or "/" in p or "\\" in p for p in parts):  # 目录穿越直接拒
                    return self.send_json({"error": "not found"}, 404)
                if os.path.isfile(os.path.join(ui, *parts)):
                    return self.send_ui_file(os.path.join(*parts))
                return self.send_page(os.path.join(ui, "index.html"))  # 前端路由：找不到的路径回退到入口页
            if parts == ["api", "sessions"]:
                return self.send_json({"now": store.now(), "sessions": store.session_rows()})
            if parts == ["api", "batches"]:
                return self.send_json({"now": store.now(), "batches": store.batch_rows(query["project"][0] if query.get("project") else None)})
            if parts == ["api", "overview"]:
                return self.send_json(overview())
            if parts == ["api", "version"]:  # 很轻：给页面横幅和 web status 判断服务是否旧版本
                return self.send_json(version_info())
            if parts == ["api", "config"]:
                return self.send_json(config_overview())
            if len(parts) == 4 and parts[:3] == ["api", "config", "roles"]:
                project = definitions.load_project(query["project"][0]) if query.get("project") else None
                return self.send_json(definitions.role_detail(project, parts[3]))
            if parts == ["api", "mcp"]:
                page = registry_page("mcp", query.get("project", [""])[0])
                return self.send_json({"mcp": page.pop("entries"), **page})
            if parts == ["api", "mcp", "candidates"]:
                return self.send_json({"candidates": registry_candidates("mcp", query.get("project", [None])[0])})
            if parts == ["api", "skills"]:
                page = registry_page("skills", query.get("project", [""])[0])
                return self.send_json({"skills": page.pop("entries"), **page})
            if parts == ["api", "skills", "candidates"]:
                return self.send_json({"candidates": registry_candidates("skills", query.get("project", [None])[0])})
            if parts == ["api", "dispatch-options"]:
                return self.send_json(dispatch_options())
            if parts == ["api", "models"]:
                return self.send_json(model_page())
            if parts == ["api", "models", "candidates"]:  # 先看再选：工具里现有的模型及登记情况
                backend = query["backend"][0] if query.get("backend") else None
                return self.send_json({"candidates": models.discover_candidates(backend)})
            if len(parts) >= 3 and parts[:2] == ["api", "job"]:
                name = parts[2]
                store.check_name(name)
                if len(parts) == 3:
                    return self.send_json(job_detail(name))
                if parts[3] == "log":
                    return self.send_json(read_log(name, int(query.get("offset", ["-1"])[0])))
                if parts[3] == "file":
                    return self.send_run_file(name, query.get("path", [""])[0])
            self.send_json({"error": "not found"}, 404)
        except store.DispatchError as exc:
            self.send_json({"error": str(exc)}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def writable(self):
        """写操作只接受本页面发出的请求：要求自定义请求头，跨站表单和脚本带不上它。"""
        if self.headers.get("X-Agent-Dispatch") != "1":
            self.send_json({"error": "拒绝：缺少请求头"}, 403)
            return False
        origin = self.headers.get("Origin")
        if origin and urllib.parse.urlparse(origin).hostname not in ("127.0.0.1", "localhost"):
            self.send_json({"error": "拒绝：来源不是本机"}, 403)
            return False
        return True

    def body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}") if length else {}

    def run_job_action(self, name, args, stdin_text=None, gate=False):
        """对单个作业跑一条命令：非 0 返回 400；gate 的返回码 1 表示闸门未过，不算接口错误。"""
        code, text, error, _ = run_cli(args, stdin_text)
        if code is None:
            return self.send_json({"ok": False, "passed": gate and False, "message": error}, 500)
        if gate:
            return self.send_json({"ok": True, "passed": code == 0, "message": text})
        if code != 0:
            return self.send_json({"ok": False, "message": error, "error": error}, 400)
        return self.send_json({"ok": True, "message": text})

    def post_job(self, data):
        """派工：作业名可省，省了就按角色和时间生成。"""
        job, task, args = dispatch_start_args(data)
        code, text, error, _ = run_cli(args, task)
        if code is None:
            return self.send_json({"ok": False, "message": error}, 500)
        if code != 0:
            return self.send_json({"ok": False, "job": job, "message": error, "error": error}, 400)
        return self.send_json({"ok": True, "job": job, "message": text})

    def post_preview(self, data):
        """派工预览：同一套参数跑子进程 dry-run，返回不真正派出的 JSON。只解析标准输出， stderr 里的提示不影响。"""
        _, task, args = dispatch_start_args(data)
        code, _, error, out = run_cli(args + ["--dry-run", "--json"], task)
        if code is None:
            return self.send_json({"ok": False, "message": error}, 500)
        if code != 0:
            return self.send_json({"ok": False, "error": error, "message": error}, 400)
        try:
            return self.send_json(json.loads(out))
        except ValueError:
            return self.send_json({"ok": False, "message": f"预览输出不是合法 JSON：{out[:500]}"}, 500)

    def do_POST(self):
        if not self.writable():
            return
        parts = [urllib.parse.unquote(p) for p in urllib.parse.urlparse(self.path).path.split("/") if p]
        try:
            data = self.body()
            if parts == ["api", "models", "import"]:
                names = data.get("names")
                if names is not None and (not isinstance(names, list) or not all(isinstance(n, str) for n in names)):
                    raise store.DispatchError("names 必须是模型 ID 的数组")
                added = (models.import_named(names, data.get("backend") or None) if names
                         else models.import_all(data.get("backend") or None))
                return self.send_json({"added": added})
            if parts == ["api", "models", "settings"]:  # 全局默认并发上限
                from . import runner
                value = data.get("default_max_concurrent")
                if value is None:
                    raise store.DispatchError("要有 default_max_concurrent（≥1 的整数）")
                models.set_default_limit(models.check_limit(value))
                runner.promote_queued()
                return self.send_json({"ok": True})
            if parts == ["api", "models"]:
                models.add(data.get("alias", "").strip(), data.get("backend", ""), data.get("id", "").strip(), data.get("note", ""))
                return self.send_json({"ok": True})
            if len(parts) == 4 and parts[:2] == ["api", "models"] and parts[3] == "resume":
                models.resume(parts[2])
                return self.send_json({"ok": True})
            if len(parts) == 3 and parts[:2] == ["api", "models"]:
                if "max_concurrent" in data:  # null 表示恢复用全局默认值
                    value = data["max_concurrent"]
                    models.set_limit(parts[2], models.check_limit(value) if value is not None else None)
                    from . import runner
                    runner.promote_queued()
                models.update(parts[2], enabled=data.get("enabled"), note=data.get("note"))
                return self.send_json({"ok": True})
            if len(parts) == 3 and parts[:2] == ["api", "backends"]:
                backends.set_enabled(parts[2], bool(data.get("enabled")))
                return self.send_json({"ok": True})
            if parts == ["api", "mcp"]:
                if not data.get("name") or not isinstance(data.get("entry"), dict):
                    raise store.DispatchError("要有 name（登记 ID）和 entry（条目定义）")
                registry.save_mcp(data["name"].strip(), data["entry"], project=data.get("project") or None)
                return self.send_json({"ok": True})
            if parts == ["api", "mcp", "import"]:
                selections = data.get("selections") or data.get("names") or []
                if not isinstance(selections, list) or not selections:
                    raise store.DispatchError("要有 names（登记 ID / 服务名数组）或 selections（[{name, source}]）")
                if data.get("project"):
                    definitions.load_project(data["project"])  # 项目不存在时给出明确错误
                return self.send_json({"added": registry.import_mcp(selections, project=data.get("project") or None)})
            if parts == ["api", "skills"]:
                if not data.get("name") or not data.get("path"):
                    raise store.DispatchError("要有 name（登记 ID）和 path（技能目录）")
                registry.save_skill(data["name"].strip(), data["path"], data.get("skill_name"),
                                    project=data.get("project") or None)
                return self.send_json({"ok": True})
            if parts == ["api", "skills", "import"]:
                selections = data.get("selections") or data.get("names") or []
                if not isinstance(selections, list) or not selections:
                    raise store.DispatchError("要有 names（登记 ID / 技能名数组）或 selections（[{name, source}]）")
                if data.get("project"):
                    definitions.load_project(data["project"])
                return self.send_json({"added": registry.import_skills(selections, project=data.get("project") or None)})
            if parts == ["api", "pools"]:
                project = definitions.load_project(data["project"]) if data.get("project") else None
                pool = data.get("pool") or []
                definitions.set_pool(project, data.get("role", ""), pool or None)
                return self.send_json({"ok": True})
            if parts == ["api", "config", "roles"]:
                project = config_project(data)
                path, inherits = definitions.add_role(data.get("id", ""), project, data.get("title"),
                                                      data.get("description"), data.get("repos"),
                                                      data.get("backend"), data.get("mode"))
                return self.send_json({"ok": True, "path": path, "inherits": inherits})
            if len(parts) == 4 and parts[:3] == ["api", "config", "roles"]:
                role_id, project = parts[3], config_project(data)
                if "fields" in data:
                    if not isinstance(data["fields"], dict):
                        raise store.DispatchError("fields 必须是「字段名: 值」的对象")
                    definitions.set_role_fields(project, role_id, data["fields"])
                elif "reset" in data:
                    if not isinstance(data["reset"], list) or not all(isinstance(k, str) for k in data["reset"]):
                        raise store.DispatchError("reset 必须是字段名的数组")
                    definitions.reset_role_fields(project, role_id, data["reset"])
                else:
                    raise store.DispatchError("请求体里要有 fields（写字段）或 reset（恢复继承）")
                return self.send_json({"ok": True})
            if len(parts) == 5 and parts[:3] == ["api", "config", "roles"] and parts[4] == "brief":
                project = config_project(data)
                if data.get("reset"):
                    definitions.reset_brief(project, parts[3])
                else:
                    definitions.set_brief(project, parts[3], data.get("text") or "")
                return self.send_json({"ok": True})
            if parts == ["api", "config", "projects"]:
                path = definitions.add_project(data.get("id", ""), data.get("name"), data.get("repos"))
                return self.send_json({"ok": True, "path": path})
            if len(parts) == 4 and parts[:3] == ["api", "config", "projects"]:
                definitions.update_project(parts[3], data.get("name"), data.get("default_role"), data.get("defaults"))
                return self.send_json({"ok": True})
            if len(parts) == 5 and parts[:3] == ["api", "config", "projects"] and parts[4] == "repos":
                channels = data.get("channels")
                if isinstance(channels, str):  # 网页表单里通道常写成逗号分隔的一个字符串
                    channels = [c.strip() for c in channels.split(",") if c.strip()]
                definitions.add_repo(parts[3], data.get("id", ""), data.get("cwd", ""), channels,
                                     data.get("channel_namespace"))
                return self.send_json({"ok": True})
            if len(parts) == 5 and parts[:3] == ["api", "config", "projects"] and parts[4] == "supervisor":
                path = definitions.set_supervisor_notes(parts[3], data.get("text") or "")
                return self.send_json({"ok": True, "path": path})
            if parts == ["api", "jobs"]:
                return self.post_job(data)
            if len(parts) == 3 and parts[:2] == ["api", "batches"]:
                batch = parts[2]
                if not any(j.get("batch") == batch for j in store.all_jobs()) and not store.load_batch(batch):
                    raise store.DispatchError(f"没有这个批次：{batch}")
                store.register_batch(batch, (data.get("title") or "").strip() or None,
                                     (data.get("reason") or "").strip() or None)
                return self.send_json({"ok": True, "batch": store.load_batch(batch)})
            if parts == ["api", "jobs", "preview"]:
                return self.post_preview(data)
            if len(parts) == 4 and parts[:2] == ["api", "jobs"]:
                name, action = parts[2], parts[3]
                store.check_name(name)
                if action == "reply":
                    text = (data.get("text") or "").strip()
                    if not text:
                        return self.send_json({"ok": False, "error": "内容为空"}, 400)
                    return self.run_job_action(name, ["reply", name, "-"], text)
                if action == "stop":
                    args = ["stop", name] + ([f"--reason={data['reason']}"] if data.get("reason") else [])
                    return self.run_job_action(name, args)
                if action == "gate":
                    return self.run_job_action(name, ["gate", name], gate=True)
                if action == "accept":
                    args = ["accept", name] + ([f"--note={data['note']}"] if data.get("note") else [])
                    return self.run_job_action(name, args)
                if action == "reject":
                    reason = (data.get("reason") or "").strip()
                    if not reason:
                        return self.send_json({"ok": False, "error": "驳回必须写原因"}, 400)
                    return self.run_job_action(name, ["reject", name, f"--reason={reason}"])
                if action == "clear-escalation":
                    return self.run_job_action(name, ["escalate", name, "--clear"])
            self.send_json({"error": "not found"}, 404)
        except (store.DispatchError, ValueError) as exc:
            self.send_json({"error": str(exc)}, 400)

    def do_DELETE(self):
        if not self.writable():
            return
        parts = [urllib.parse.unquote(p) for p in urllib.parse.urlparse(self.path).path.split("/") if p]
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        try:
            if len(parts) == 3 and parts[:2] == ["api", "models"]:
                models.remove(parts[2])
                return self.send_json({"ok": True})
            if len(parts) == 3 and parts[0] == "api" and parts[1] in ("mcp", "skills"):
                (registry.remove_mcp if parts[1] == "mcp" else registry.remove_skill)(parts[2])
                return self.send_json({"ok": True})
            if len(parts) == 4 and parts[:3] == ["api", "config", "roles"]:
                project = definitions.load_project(query["project"][0]) if query.get("project") else None
                path = definitions.delete_role_layer(project, parts[3])
                return self.send_json({"ok": True, "path": path})
            if len(parts) == 6 and parts[:3] == ["api", "config", "projects"] and parts[4] == "repos":
                definitions.remove_repo(parts[3], parts[5])
                return self.send_json({"ok": True})
            self.send_json({"error": "not found"}, 404)
        except store.DispatchError as exc:
            self.send_json({"error": str(exc)}, 400)

    def send_run_file(self, name, relative):
        """只允许读该作业 run 目录之内的文件。"""
        job = store.load(name)
        run_dir = run_dir_of(job, profile_of(job))
        if not run_dir or not relative:
            return self.send_json({"error": "没有 run 目录"}, 404)
        path = os.path.realpath(os.path.join(run_dir, relative))
        if not path.startswith(os.path.realpath(run_dir) + os.sep) or not os.path.isfile(path):
            return self.send_json({"error": "文件不存在"}, 404)
        if path.lower().endswith(TEXT_SUFFIXES):
            with open(path, "rb") as f:
                return self.send_body(f.read(2_000_000), "text/plain; charset=utf-8")
        with open(path, "rb") as f:
            return self.send_body(f.read(), mimetypes.guess_type(path)[0] or "application/octet-stream")


def serve(host, port):
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    print(f"派工总览：http://{host}:{port}/    状态目录：{store.HOME}    （Ctrl-C 关闭，不影响任何作业）", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
