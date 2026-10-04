"""模型登记与按比例选择。

模型登记表：~/.agent-dispatch/models.json
  {"cooldown_min": 30,
   "models": {"<短名>": {"backend": "pi", "id": "<provider>/<model>", "enabled": true, "note": ""}}}
暂停记录：  ~/.agent-dispatch/model-health.json   {"<短名>": {"until": 时间戳, "reason": "...", "at": "..."}}

角色里写模型池：
  "model_pool": [{"model": "<短名>", "weight": 70}, {"model": "<短名>", "weight": 30}]
派工时按历史严格配比选：选「目标占比 × (已派数 + 1) − 已派数」最大的那个，长期看严格贴合比例。
停用的、暂停中的模型自动跳过。
"""
import contextlib
import fcntl
import json
import os
import re
import subprocess
import time

from . import backends, store
from .store import DispatchError

REGISTRY = os.path.join(store.HOME, "models.json")
HEALTH = os.path.join(store.HOME, "model-health.json")
DEFAULT_COOLDOWN_MIN = 30
ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+-]*$")

# 模型服务商这一类错误（额度、限流、鉴权、计费），才触发暂停与换模型；任务本身的失败不算
PROVIDER_ERROR_RE = re.compile(
    r"quota|rate.?limit|throttl|too many requests|\b429\b|\b401\b|\b403\b|unauthori[sz]ed|forbidden|"
    r"provider\.(auth|quota)|free tier|insufficient|balance|credit|billing|overloaded|api key|"
    r"额度|余额|限流|欠费|鉴权|频率", re.I)


@contextlib.contextmanager
def _locked():
    os.makedirs(store.HOME, exist_ok=True)
    with open(os.path.join(store.HOME, ".models.lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _read(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except ValueError as exc:
        raise DispatchError(f"{path} 不是合法的 JSON：{exc}")


def _write(path, data):
    store.write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def load_registry():
    data = _read(REGISTRY, {})
    data.setdefault("cooldown_min", DEFAULT_COOLDOWN_MIN)
    data.setdefault("models", {})
    return data


def get(alias):
    model = load_registry()["models"].get(alias)
    if not model:
        raise DispatchError(f"模型登记表里没有 {alias!r}。查看：agent-dispatch models")
    return model


# ---------- 登记表维护 ----------

def add(alias, backend, model_id, note="", enabled=True):
    if not ALIAS_RE.match(alias):
        raise DispatchError(f"模型短名只能包含字母数字和 . _ : @ / + -：{alias!r}")
    backends.get(backend)
    with _locked():
        data = load_registry()
        data["models"][alias] = {**data["models"].get(alias, {}), "backend": backend, "id": model_id,
                                 "enabled": enabled, "note": note or data["models"].get(alias, {}).get("note", "")}
        _write(REGISTRY, data)


def update(alias, **fields):
    with _locked():
        data = load_registry()
        if alias not in data["models"]:
            raise DispatchError(f"模型登记表里没有 {alias!r}")
        for key, value in fields.items():
            if value is not None:
                data["models"][alias][key] = value
        _write(REGISTRY, data)


def remove(alias):
    users = [f"{p}:{r}" for p, r, pool in all_pools() if any(e.get("model") == alias for e in pool)]
    if users:
        raise DispatchError(f"模型 {alias} 还在这些角色的模型池里：{'、'.join(users)}。先从池子里移除，或改为停用")
    with _locked():
        data = load_registry()
        if data["models"].pop(alias, None) is None:
            raise DispatchError(f"模型登记表里没有 {alias!r}")
        _write(REGISTRY, data)


def discover():
    """问各后端现在有哪些模型：[(backend, provider/model)]。"""
    found = []
    try:
        out = subprocess.run(["pi", "--list-models"], capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL).stdout
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2:
                found.append(("pi", f"{parts[0]}/{parts[1]}"))
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        out = subprocess.run(["opencode", "models"], capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL).stdout
        found += [("opencode", line.strip()) for line in out.splitlines() if "/" in line and " " not in line.strip()]
    except (OSError, subprocess.TimeoutExpired):
        pass
    return found


def import_all():
    """把各后端现有的模型都登记进来（已登记的不动），返回新增的短名。"""
    added = []
    with _locked():
        data = load_registry()
        known = {(m["backend"], m["id"]) for m in data["models"].values()}
        found = discover()
        names = {}
        for backend, model_id in found:  # 同名模型出现在多个服务商下时，短名带上服务商
            names.setdefault((backend, model_id.rpartition("/")[2]), []).append(model_id)
        for backend, model_id in found:
            if (backend, model_id) in known:
                continue
            prefix = "oc" if backend == "opencode" else backend
            provider, _, name = model_id.rpartition("/")
            alias = f"{prefix}:{name}" if len(names[(backend, name)]) == 1 else f"{prefix}:{name}@{provider}"
            if alias in data["models"]:
                alias = f"{prefix}:{name}@{provider}"
            data["models"][alias] = {"backend": backend, "id": model_id, "enabled": True, "note": ""}
            known.add((backend, model_id))
            added.append(alias)
        _write(REGISTRY, data)
    return added


# ---------- 暂停（服务商出错后一段时间内不再选它） ----------

def health():
    now = time.time()
    return {alias: h for alias, h in _read(HEALTH, {}).items() if h.get("until", 0) > now}


def pause(alias, reason, minutes=None):
    minutes = minutes if minutes is not None else load_registry()["cooldown_min"]
    with _locked():
        data = {k: v for k, v in _read(HEALTH, {}).items() if v.get("until", 0) > time.time()}
        data[alias] = {"until": time.time() + minutes * 60, "reason": reason[:300], "at": store.now()}
        _write(HEALTH, data)


def resume(alias):
    with _locked():
        data = _read(HEALTH, {})
        data.pop(alias, None)
        _write(HEALTH, data)


def alias_of(backend, model_id):
    for alias, model in load_registry()["models"].items():
        if model["backend"] == backend and model["id"] == model_id:
            return alias
    return None


def is_provider_error(text):
    return bool(text and PROVIDER_ERROR_RE.search(text))


# ---------- 模型池与选择 ----------

def all_pools():
    """[(项目ID, 角色ID, 模型池)]：所有配置了模型池的角色（含全局角色，项目ID 为空）。"""
    from . import definitions
    out = []
    for role in definitions.list_roles(None):
        if role.get("model_pool"):
            out.append(("", role["role"], role["model_pool"]))
    for project in definitions.list_projects():
        for role in definitions.list_roles(project):
            if role.get("pool_source") == "项目":
                out.append((project["id"], role["role"], role["model_pool"]))
    return out


def check_pool(pool):
    if not isinstance(pool, list) or not pool:
        raise DispatchError("模型池不能为空")
    registry = load_registry()["models"]
    seen = set()
    for entry in pool:
        alias, weight = entry.get("model"), entry.get("weight")
        if alias not in registry:
            raise DispatchError(f"模型池里的 {alias!r} 不在模型登记表里")
        if alias in seen:
            raise DispatchError(f"模型池里 {alias} 重复了")
        if not isinstance(weight, (int, float)) or weight <= 0:
            raise DispatchError(f"模型 {alias} 的比例必须是正数")
        seen.add(alias)


def usage(project_id, role_id, any_project=False):
    """这个角色历史上每个模型派过多少个作业（自动换模型前的尝试也算，它确实占用了那个模型）。
    配比按「项目 + 角色」分别保持；any_project=True 时汇总所有项目（网页上看全局角色用）。"""
    counts = {}
    for job in store.all_jobs():
        if job.get("role") != role_id or (not any_project and (job.get("project_id") or "") != (project_id or "")):
            continue
        for attempt in job.get("model_attempts") or []:
            counts[attempt["model"]] = counts.get(attempt["model"], 0) + 1
        if job.get("model_alias"):
            counts[job["model_alias"]] = counts.get(job["model_alias"], 0) + 1
    return counts


def candidates(pool, exclude=()):
    """池子里现在能用的模型，以及不能用的原因。"""
    registry, paused = load_registry()["models"], health()
    usable, skipped = [], {}
    for entry in pool:
        alias = entry["model"]
        model = registry.get(alias)
        if alias in exclude:
            skipped[alias] = "本作业已试过"
        elif not model:
            skipped[alias] = "不在登记表里"
        elif not model.get("enabled", True):
            skipped[alias] = "已停用"
        elif not backends.enabled(model["backend"]):
            skipped[alias] = f"派工工具 {model['backend']} 已停用"
        elif alias in paused:
            skipped[alias] = f"暂停中（{paused[alias]['reason'][:60]}）"
        else:
            usable.append(entry)
    return usable, skipped


def pick(pool, project_id, role_id, exclude=()):
    """按历史严格配比选一个模型，返回 (短名, 登记信息)。"""
    usable, skipped = candidates(pool, exclude)
    if not usable:
        detail = "；".join(f"{alias}：{why}" for alias, why in skipped.items())
        raise DispatchError(f"角色 {role_id} 的模型池里现在没有可用的模型（{detail}）。请到网页或用 agent-dispatch models 处理")
    counts = usage(project_id, role_id)
    total_weight = sum(e["weight"] for e in usable)
    total = sum(counts.get(e["model"], 0) for e in usable)
    best = max(usable, key=lambda e: e["weight"] / total_weight * (total + 1) - counts.get(e["model"], 0))
    return best["model"], load_registry()["models"][best["model"]]
