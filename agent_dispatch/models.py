"""模型登记与按比例选择。

模型登记表：~/.agent-dispatch/models.json
  {"cooldown_min": 30, "default_max_concurrent": 5,
   "models": {"<短名>": {"backend": "pi", "id": "<provider>/<model>", "enabled": true, "note": "",
                "max_concurrent": 3}}}   # max_concurrent 可缺省 = 用全局默认；同一个 (工具, 模型ID) 共用一份并发名额}
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
DEFAULT_MAX_CONCURRENT = 5   # 同一个模型（按 工具+模型ID 计）同时最多承接几张派工单
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
    data.setdefault("default_max_concurrent", DEFAULT_MAX_CONCURRENT)
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


# ---------- 并发上限 ----------

class ModelBusy(DispatchError):
    """模型池里可用的模型都达到了并发上限：调用方应把作业排队而不是报错。"""

    def __init__(self, waiting):
        self.waiting = waiting  # [(backend, 模型ID)]：在等哪些模型空出来
        super().__init__("模型池里的模型并发都已满")


def check_limit(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise DispatchError("并发上限必须是 ≥1 的整数")
    return value


def set_limit(alias, value):
    """设置单个模型的并发上限；value 为 None 表示恢复用全局默认值。"""
    with _locked():
        data = load_registry()
        if alias not in data["models"]:
            raise DispatchError(f"模型登记表里没有 {alias!r}")
        if value is None:
            data["models"][alias].pop("max_concurrent", None)
        else:
            data["models"][alias]["max_concurrent"] = check_limit(value)
        _write(REGISTRY, data)


def set_default_limit(value):
    with _locked():
        data = load_registry()
        data["default_max_concurrent"] = check_limit(value)
        _write(REGISTRY, data)


def occupancy():
    """每个 (工具, 模型ID) 当前被多少个运行中的作业占用（多个短名指向同一模型时共用一份）。"""
    counts = {}
    for job in store.all_jobs():
        if store.effective_state(job) != "running":
            continue
        key = (job.get("backend"), job.get("model"))
        counts[key] = counts.get(key, 0) + 1
    return counts


def limit_for(key, registry=None):
    """某个 (工具, 模型ID) 的并发上限：任一指向它的短名单独设了上限就用它，否则用全局默认值。"""
    registry = registry or load_registry()
    for model in registry["models"].values():
        if (model["backend"], model["id"]) == key and model.get("max_concurrent"):
            return model["max_concurrent"]
    return registry.get("default_max_concurrent", DEFAULT_MAX_CONCURRENT)


def full_keys(registry=None):
    """已达到并发上限的 (工具, 模型ID) 集合。"""
    registry = registry or load_registry()
    counts = occupancy()
    keys = set(counts) | {(m["backend"], m["id"]) for m in registry["models"].values()}
    return {key for key in keys if counts.get(key, 0) >= limit_for(key, registry)}


def busy(backend, model_id):
    """指定的模型现在是否已满（固定模型的派工 / 续派用）。"""
    key = (backend, model_id)
    return occupancy().get(key, 0) >= limit_for(key)


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


def _import_plan(found, data):
    """把发现的模型变成待登记的 (短名, 后端, 模型ID)：已登记的跳过，同名模型带上服务商。"""
    known = {(m["backend"], m["id"]) for m in data["models"].values()}
    names = {}
    for backend, model_id in found:  # 同名模型出现在多个服务商下时，短名带上服务商
        names.setdefault((backend, model_id.rpartition("/")[2]), []).append(model_id)
    plan = []
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
        plan.append(alias)
    return plan


def import_all(backend=None):
    """把某个后端（不指定则全部）现有的模型登记进来（已登记的不动），返回新增的短名。"""
    with _locked():
        data = load_registry()
        found = [item for item in discover() if backend is None or item[0] == backend]
        added = _import_plan(found, data)
        if added:
            _write(REGISTRY, data)
    return added


def import_named(names, backend=None):
    """只登记指名时模型 ID 的候选（找不到的报错），返回新增的短名。"""
    want = set(names)
    found = [item for item in discover() if item[1] in want and (backend is None or item[0] == backend)]
    missing = want - {item[1] for item in found}
    if missing:
        raise DispatchError(f"工具里没有这些模型：{'、'.join(sorted(missing))}。先看候选：agent-dispatch models import --list")
    with _locked():
        data = load_registry()
        added = _import_plan(found, data)
        if added:
            _write(REGISTRY, data)
    return added


def discover_candidates(backend=None):
    """先看再选：各工具现在有哪些模型、哪些已登记。[(backend, 模型ID, 已登记短名或None)]。"""
    registry = load_registry()["models"]
    known = {(m["backend"], m["id"]): alias for alias, m in registry.items()}
    return [{"backend": b, "id": model_id, "registered": (b, model_id) in known, "alias": known.get((b, model_id))}
            for b, model_id in discover() if backend is None or b == backend]


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
    """池子里现在能用的模型，以及不能用的原因（含并发已满）。"""
    registry, paused = load_registry(), health()
    full = full_keys(registry)
    usable, skipped = [], {}
    for entry in pool:
        alias = entry["model"]
        model = registry["models"].get(alias)
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
        elif (model["backend"], model["id"]) in full:
            skipped[alias] = "并发已满"
        else:
            usable.append(entry)
    return usable, skipped


def pick(pool, project_id, role_id, exclude=()):
    """按历史严格配比选一个模型，返回 (短名, 登记信息)。可用的都达到了并发上限时抛 ModelBusy。"""
    usable, skipped = candidates(pool, exclude)
    if not usable:
        waiting = [(load_registry()["models"][a]["backend"], load_registry()["models"][a]["id"])
                   for a in skipped if skipped[a] == "并发已满"]
        if waiting and all(why == "并发已满" for why in skipped.values()):
            raise ModelBusy(waiting)
        detail = "；".join(f"{alias}：{why}" for alias, why in skipped.items())
        raise DispatchError(f"角色 {role_id} 的模型池里现在没有可用的模型（{detail}）。请到网页或用 agent-dispatch models 处理")
    counts = usage(project_id, role_id)
    total_weight = sum(e["weight"] for e in usable)
    total = sum(counts.get(e["model"], 0) for e in usable)
    best = max(usable, key=lambda e: e["weight"] / total_weight * (total + 1) - counts.get(e["model"], 0))
    return best["model"], load_registry()["models"][best["model"]]
