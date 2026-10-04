"""本地网页总览：作业部分只读；「模型与角色」页可以管理模型登记表和角色的模型池。关掉不影响派工。"""
import json
import mimetypes
import os
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import backends, cli, definitions, gates, models, store, tmux

PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web.html")
TEXT_SUFFIXES = (".md", ".json", ".jsonl", ".txt", ".log", ".sql", ".csv")


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


def summarize(job):
    state = store.effective_state(job)
    start = job.get("turn_started_ts")
    end = time.time() if state == "running" else job.get("ended_ts") or start
    group, project = store.project_of(job)
    return {
        "name": job["name"], "batch": job.get("batch") or "", "profile": job.get("profile"),
        "group": group, "project": project, "role": job.get("role"), "role_title": job.get("role_title"),
        "backend": job.get("backend"), "model": job.get("model"), "model_alias": job.get("model_alias"),
        "model_actual": job.get("model_actual"), "state": state,
        "state_label": cli.STATE_LABEL.get(state, state), "status": job.get("status"), "turn": job.get("turn"),
        "seconds": int(max(0, end - start)) if start else None, "channels": job.get("channels") or [],
        "task": job.get("task", ""), "needs_user": job.get("needs_user"), "reason": job.get("reason"),
        "gate_ok": (job.get("gate") or {}).get("ok"), "created": job.get("created"), "mode": job.get("mode", "headless"),
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
    return {"now": store.now(), "home": store.HOME, "jobs": [summarize(j) for j in jobs], "cases": cases,
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
    for project_id, role_id, pool in models.all_pools():
        for entry in pool:
            used.setdefault(entry["model"], []).append({"project": project_id, "role": role_id, "weight": entry["weight"]})
    model_rows = []
    for alias, model in registry["models"].items():
        st = stats.get(alias, {"jobs": 0, "pass": 0, "settled": 0, "failed": 0, "seconds": 0})
        model_rows.append({"alias": alias, "backend": model["backend"], "id": model["id"], "enabled": model.get("enabled", True),
                           "note": model.get("note", ""), "paused": paused.get(alias), "used_by": used.get(alias, []),
                           "jobs": st["jobs"], "settled": st["settled"], "pass": st["pass"], "failed": st["failed"],
                           "avg_seconds": int(st["seconds"] / st["settled"]) if st["settled"] else None,
                           "provider_failures": provider_failures.get(alias, 0)})
    model_rows.sort(key=lambda m: (not m["used_by"], not m["enabled"], m["backend"], m["alias"]))
    roles = []
    for project in definitions.list_projects():
        for role in definitions.list_roles(project):
            counts = models.usage(project["id"], role["role"])
            roles.append({"project": project["id"], "project_name": project["name"], "role": role["role"], "title": role["title"],
                          "repos": role["repos"], "pool": role.get("model_pool") or [], "usage": counts,
                          "fallback": role.get("model") or (role.get("models") or {}).get(role.get("backend") or "")
                          or f"{role.get('backend') or '-'} 的默认模型"})
    return {"cooldown_min": registry["cooldown_min"], "models": model_rows, "roles": roles,
            "backends": sorted(backends.load_all())}


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
    detail = summarize(job)
    detail.update({
        "cwd": job.get("cwd"), "session_id": job.get("session_id"), "attach": attach,
        "mode": job.get("mode", "headless"), "tmux_alive": tmux.alive(job.get("tmux")),
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


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_body(self, body, content_type, status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, data, status=200):
        self.send_body(json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", status)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(url.query)
        parts = [urllib.parse.unquote(p) for p in url.path.split("/") if p]
        try:
            if not parts:
                with open(PAGE, "rb") as f:
                    return self.send_body(f.read(), "text/html; charset=utf-8")
            if parts == ["api", "overview"]:
                return self.send_json(overview())
            if parts == ["api", "models"]:
                return self.send_json(model_page())
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

    def do_POST(self):
        if not self.writable():
            return
        parts = [urllib.parse.unquote(p) for p in urllib.parse.urlparse(self.path).path.split("/") if p]
        try:
            data = self.body()
            if parts == ["api", "models", "import"]:
                return self.send_json({"added": models.import_all()})
            if parts == ["api", "models"]:
                models.add(data.get("alias", "").strip(), data.get("backend", ""), data.get("id", "").strip(), data.get("note", ""))
                return self.send_json({"ok": True})
            if len(parts) == 4 and parts[:2] == ["api", "models"] and parts[3] == "resume":
                models.resume(parts[2])
                return self.send_json({"ok": True})
            if len(parts) == 3 and parts[:2] == ["api", "models"]:
                models.update(parts[2], enabled=data.get("enabled"), note=data.get("note"))
                return self.send_json({"ok": True})
            if parts == ["api", "pools"]:
                project = definitions.load_project(data.get("project", ""))
                pool = data.get("pool") or []
                definitions.set_pool(project, data.get("role", ""), pool or None)
                return self.send_json({"ok": True})
            self.send_json({"error": "not found"}, 404)
        except (store.DispatchError, ValueError) as exc:
            self.send_json({"error": str(exc)}, 400)

    def do_DELETE(self):
        if not self.writable():
            return
        parts = [urllib.parse.unquote(p) for p in urllib.parse.urlparse(self.path).path.split("/") if p]
        try:
            if len(parts) == 3 and parts[:2] == ["api", "models"]:
                models.remove(parts[2])
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
