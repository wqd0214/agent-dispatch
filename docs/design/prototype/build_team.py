#!/usr/bin/env python3
"""把本机派工台账（只读）做成一份快照，生成「谁在给谁干活」原型页 team.html。

用法：python3 docs/design/prototype/build_team.py
只读 ~/.agent-dispatch/jobs/，不改任何台账文件。
"""
import glob
import json
import os
import time

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.environ.get("AGENT_DISPATCH_HOME") or os.path.expanduser("~/.agent-dispatch")
KEEP_EVENTS = 400  # 每张单只带最后这么多条会话事件，够预览用


def ts(text):
    try:
        return int(time.mktime(time.strptime(text, "%Y-%m-%d %H:%M:%S")))
    except (TypeError, ValueError):
        return None


def clip(text, n):
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[:n] + "…"


def last_activity(job_dir):
    """运行中的单子最后在干什么：取事件流的最后一条。"""
    try:
        with open(os.path.join(job_dir, "events.jsonl"), "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 20000))
            lines = f.read().decode("utf-8", "ignore").strip().splitlines()
        event = json.loads(lines[-1])
    except (OSError, ValueError, IndexError):
        return ""
    if event.get("kind") == "tool":
        arg = event.get("input") or {}
        detail = arg.get("command") or arg.get("path") or arg.get("file_path") or ""
        return clip(f"{event.get('name', '')} {detail}", 70)
    return clip(event.get("text"), 70)


def session_file(job_dir, name, out_dir):
    """执行者的会话内容（它说的话、调的工具、每轮收到的交代）单独存一个文件，点开那张单时才加载。"""
    events, total = [], 0
    try:
        with open(os.path.join(job_dir, "events.jsonl"), encoding="utf-8", errors="ignore") as f:
            lines = f.read().splitlines()
    except OSError:
        lines = []
    total = len(lines)
    for line in lines[-KEEP_EVENTS:]:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        base = {"t": (e.get("t") or "")[5:16], "r": e.get("turn") or 1}
        if e.get("kind") == "tool":
            arg = e.get("input") or {}
            brief = arg.get("command") or arg.get("path") or arg.get("file_path") or json.dumps(arg, ensure_ascii=False)
            out = str(e.get("output") or "")
            events.append({**base, "k": "t", "n": e.get("name"), "i": brief[:400], "o": out[:1200] + ("\n…（已截断）" if len(out) > 1200 else ""),
                           "e": bool(e.get("error")), "ms": e.get("ms") or 0})
        elif e.get("kind") == "text":
            text = str(e.get("text") or "")
            events.append({**base, "k": "x", "x": text[:3000] + ("\n…（已截断）" if len(text) > 3000 else "")})
        else:
            events.append({**base, "k": "e", "x": str(e.get("message") or "")[:600]})
    asks = {}
    for path in glob.glob(os.path.join(job_dir, "turns", "*.task.md")):
        try:
            with open(path, encoding="utf-8", errors="ignore") as f:
                text = f.read()
        except OSError:
            continue
        asks[os.path.basename(path).split(".")[0]] = text[:4000] + ("\n…（已截断）" if len(text) > 4000 else "")
    payload = {"omitted": max(0, total - KEEP_EVENTS), "asks": asks, "ev": events}
    with open(os.path.join(out_dir, name + ".js"), "w", encoding="utf-8") as f:
        f.write("__sess(%s, %s);" % (json.dumps(name), json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")))


def main():
    out_dir = os.path.join(HERE, "team-data")
    os.makedirs(out_dir, exist_ok=True)
    jobs = []
    for path in glob.glob(os.path.join(HOME, "jobs", "*", "job.json")):
        try:
            with open(path, encoding="utf-8") as f:
                j = json.load(f)
        except (OSError, ValueError):
            continue
        dispatcher = j.get("dispatcher") or {}
        report = j.get("report") or {}
        turns = [{"s": ts(t.get("started")), "e": ts(t.get("ended")), "st": t.get("state"), "status": t.get("status"),
                  "why": clip(t.get("reason"), 80)} for t in j.get("turns") or []]
        if j.get("state") == "running" and not any(t["e"] is None for t in turns):
            turns.append({"s": int(j.get("turn_started_ts") or j.get("created_ts") or 0), "e": None,
                          "st": "running", "status": None, "why": ""})  # 正在跑的这一轮还没写进 turns
        if j.get("name"):
            session_file(os.path.dirname(path), j["name"], out_dir)
        jobs.append({
            "n": j.get("name"), "g": j.get("group") or "（未归项目）", "repo": j.get("project") or "",
            "role": j.get("role_title") or "（未指定角色）", "model": j.get("model_alias") or j.get("model") or j.get("backend") or "",
            "batch": j.get("batch") or "（无批次）", "st": j.get("state"), "status": j.get("status"),
            "turn": j.get("turn") or 1, "c": int(j.get("created_ts") or 0), "need": bool(j.get("needs_user")),
            "mode": j.get("mode"), "task": clip(j.get("task"), 260),
            "sum": clip(report.get("SUMMARY") or j.get("reason"), 200),
            "q": clip(j.get("needs_user") if isinstance(j.get("needs_user"), str) else report.get("QUESTION"), 200),
            "turns": turns,
            "dec": [{"t": ts(d.get("at")), "a": d.get("action"), "note": clip(d.get("note"), 220)} for d in j.get("decisions") or []],
            "last": last_activity(os.path.dirname(path)) if j.get("state") == "running" else "",
            # 派工者：新单子有登记就用登记的会话；历史单子没有，暂按批次当作一次会话
            "sk": dispatcher.get("session_id") or "batch:" + (j.get("batch") or ""),
            "skind": dispatcher.get("kind") or "",
            "stitle": dispatcher.get("title") or "",
        })
    jobs.sort(key=lambda x: x["c"])
    data = {"now": int(time.time()), "at": time.strftime("%Y-%m-%d %H:%M"), "jobs": jobs}
    with open(os.path.join(HERE, "team.template.html"), encoding="utf-8") as f:
        page = f.read()
    out = os.path.join(HERE, "team.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(page.replace("__DATA__", json.dumps(data, ensure_ascii=False).replace("</", "<\\/")))
    print(f"{len(jobs)} 张派工单 → {out}")


if __name__ == "__main__":
    main()
