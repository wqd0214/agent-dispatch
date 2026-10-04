"""机械闸门：只检查能用文件和退出码判定的事实。闸门通过 ≠ 合格，复核与裁决是主管的事。"""
import glob
import json
import os
import re

VERDICTS = {"PASS", "FAIL", "BLOCKED"}


def resolve_run_dir(job, profile):
    """优先用智能体自报的 RUN_DIR；没报就取本作业创建之后新出现的最新 run 目录。"""
    cwd = job["cwd"]
    reported = (job.get("report") or {}).get("RUN_DIR")
    if reported:
        path = reported if os.path.isabs(reported) else os.path.join(cwd, reported)
        if os.path.isdir(path):
            return os.path.realpath(path)
    root = profile.get("runs_root")
    if not root:
        return None
    since = job.get("created_ts", 0)
    candidates = []
    for entry in os.scandir(os.path.join(cwd, root)) if os.path.isdir(os.path.join(cwd, root)) else []:
        if entry.is_dir():
            stat = entry.stat()
            born = getattr(stat, "st_birthtime", stat.st_mtime)
            if born >= since:
                candidates.append((born, entry.path))
    return os.path.realpath(max(candidates)[1]) if candidates else None


def playbook_covers(run_dir):
    """读 run 目录里剧本副本的 frontmatter，取 covers 声明的用例列表。"""
    for path in sorted(glob.glob(os.path.join(run_dir, "*.playbook.md"))):
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
        match = re.match(r"^---\n(.*?)\n---", text, re.S)
        if not match:
            continue
        block = re.search(r"^covers:[ \t]*\n((?:[ \t]+-[^\n]*\n?)+)", match.group(1) + "\n", re.M)
        if block:
            return [line.split("-", 1)[1].split("#")[0].strip() for line in block.group(1).splitlines() if "-" in line]
    return []


def _check(gate, level, ok, detail):
    return {"gate": gate, "level": level, "ok": bool(ok), "detail": detail}


def gate_exit_ok(job, profile, spec, run_dir):
    settled = job.get("state") in ("done", "accepted")
    if job.get("mode") == "tmux":  # 界面不退出，没有退出码；以回合是否正常结束为准
        return _check("回合正常结束", spec["level"], settled, job.get("reason") or "界面里的回合已结束")
    ok = settled and job.get("exit_code") == 0
    return _check("进程正常结束", spec["level"], ok, job.get("reason") or f"退出码 {job.get('exit_code')}")


def gate_status_reported(job, profile, spec, run_dir):
    allowed = profile.get("statuses") or []
    status = job.get("status")
    ok = bool(status) and (not allowed or status in allowed)
    return _check("结论含 STATUS", spec["level"], ok, status or f"最后一条回复里没有 STATUS 行（应为 {' / '.join(allowed)}）")


def gate_run_dir(job, profile, spec, run_dir):
    if not run_dir:
        return _check("run 目录存在", spec["level"], False, "既没自报 RUN_DIR，也没发现本次新建的 run 目录")
    inside = run_dir.startswith(os.path.realpath(job["cwd"]) + os.sep)
    return _check("run 目录存在", spec["level"], inside, os.path.relpath(run_dir, job["cwd"]) if inside else f"{run_dir} 不在工程目录内")


def gate_file(job, profile, spec, run_dir):
    label = f"文件 {spec['path'].replace('{run_dir}/', '')}"
    if "{run_dir}" in spec["path"] and not run_dir:
        return _check(label, spec["level"], False, "没有 run 目录")
    path = spec["path"].replace("{run_dir}", run_dir or "")
    path = path if os.path.isabs(path) else os.path.join(job["cwd"], path)
    ok = os.path.isfile(path) and os.path.getsize(path) > 0
    return _check(label, spec["level"], ok, "已落盘" if ok else "缺失或为空")


def gate_verdicts(job, profile, spec, run_dir):
    """剧本声明 covers 时必须有 verdicts.json；返回多条检查。"""
    label = "用例判定 verdicts.json"
    if not run_dir:
        return _check(label, spec["level"], False, "没有 run 目录")
    covers = playbook_covers(run_dir)
    path = os.path.join(run_dir, "verdicts.json")
    if not os.path.isfile(path):
        if covers:
            return _check(label, spec["level"], False, f"剧本声明了 {len(covers)} 个用例但没有回填判定")
        return _check(label, spec["level"], True, "剧本未声明 covers，无需回填")
    try:
        with open(path, encoding="utf-8") as f:
            verdicts = json.load(f).get("verdicts") or []
    except (ValueError, AttributeError) as exc:
        return _check(label, spec["level"], False, f"无法解析：{exc}")
    bad = [v for v in verdicts if not isinstance(v, dict) or v.get("verdict") not in VERDICTS or not v.get("tc")]
    if bad:
        return _check(label, spec["level"], False, f"{len(bad)} 条判定格式不对（verdict 须为 PASS/FAIL/BLOCKED 且带 tc）")
    counts = {k: sum(1 for v in verdicts if v["verdict"] == k) for k in sorted(VERDICTS)}
    checks = [_check(label, spec["level"], True, "  ".join(f"{k} {n}" for k, n in counts.items()))]
    missing = [tc for tc in covers if tc not in {v["tc"] for v in verdicts}]
    if missing:
        checks.append(_check("用例判定覆盖完整", "warn", False, "未回填：" + "、".join(missing)))
    no_evidence = [v["tc"] for v in verdicts if v["verdict"] == "PASS" and not v.get("evidence")]
    if no_evidence:
        checks.append(_check("PASS 判定带证据锚点", "warn", False, "缺证据：" + "、".join(no_evidence)))
    failed = [v["tc"] for v in verdicts if v["verdict"] != "PASS"]
    if job.get("status") == "PASS" and failed:
        checks.append(_check("自报结论与用例判定一致", "warn", False, "自报 PASS，但这些用例不是 PASS：" + "、".join(failed)))
    return checks


GATES = {"exit_ok": gate_exit_ok, "status_reported": gate_status_reported, "run_dir": gate_run_dir,
         "file": gate_file, "verdicts": gate_verdicts}


def run_gates(job, profile):
    run_dir = resolve_run_dir(job, profile)
    checks = []
    skipped = False
    for spec in profile.get("gates") or []:
        spec = {"level": "hard", **spec}
        if spec.get("when") == "run_dir" and not run_dir:  # 只对产生了 run 目录的任务生效（如跑场景，而不是写剧本）
            skipped = True
            continue
        result = GATES[spec["type"]](job, profile, spec, run_dir)
        checks.extend(result if isinstance(result, list) else [result])
    if skipped:
        checks.append(_check("run 目录相关检查", "warn", True, "本次任务没有产生 run 目录，已跳过"))
    ok = all(c["ok"] for c in checks if c["level"] == "hard")
    return run_dir, ok, checks


def load_verdicts(run_dir):
    try:
        with open(os.path.join(run_dir, "verdicts.json"), encoding="utf-8") as f:
            data = json.load(f)
        return [v for v in data.get("verdicts") or [] if isinstance(v, dict)]
    except (OSError, ValueError, AttributeError):
        return []
