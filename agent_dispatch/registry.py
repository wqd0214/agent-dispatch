"""MCP 服务与技能的登记表（全局，一处登记），以及从现有 pi / OpenCode 配置导入。

登记表（键是唯一的**登记 ID**，条目里的 name 是下发给执行者时的服务/技能名，缺省等于登记 ID）：
  ~/.agent-dispatch/mcp.json    {登记ID: {name, type: "local"|"remote", command, args, env, url, headers,
                                           description, source, sources}}
  ~/.agent-dispatch/skills.json {登记ID: {name, path, paths, description, source, sources}}

同一个服务名在不同工程里内容不同是常态：导入时内容相同的多个来源合并成一条（记下全部来源），
内容不同的各自成一条，登记 ID 默认 <服务名>@<来源简称>（如 playwright@a04-test），name 保持服务名。
角色字段存登记 ID；同一个角色选中的条目里不允许出现两个相同的 name（执行者按 name 调用）。

敏感信息只存本机文件，对外（命令行输出、网页接口）遮罩：
env 里键名含 password / passwd / secret / token / key / dsn / credential 等字样的值、以及值本身
形如带凭据 URL 的；headers 的值；args 里形如密码令牌的内容。修改登记时提交遮罩占位则保留原值。

导入只读来源文件，绝不修改它们。来源根目录可用 AGENT_DISPATCH_IMPORT_HOME 指到别处（测试用）。
"""
import hashlib
import json
import os
import re

from . import store
from .store import DispatchError

MCP_REGISTRY = os.path.join(store.HOME, "mcp.json")
SKILLS_REGISTRY = os.path.join(store.HOME, "skills.json")
MASK = "••••"
# pi 的工具暴露方式（OpenCode 没有这个概念，下发时忽略）：direct = 像内置工具一样直接声明给模型
EXPOSURES = ("codemode", "deferred", "direct", "hidden")
# 登记 ID 允许 @（<服务名>@<来源简称>）与路径里常见的字符
REG_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@/+-]*$")

# 看起来像密码 / 令牌的参数：key=value 形式按 key 判断，URL 带凭据的整体遮掉
SECRET_KEY_RE = re.compile(r"(?i)(password|passwd|token|secret|api[_-]?key|authorization)")
# env 的键名：比 args 宽（key / dsn / credential / auth 等也算），但不是全部 env 都遮
ENV_SECRET_KEY_RE = re.compile(r"(?i)(password|passwd|secret|token|key|dsn|credential|auth)")
CREDENTIAL_URL_RE = re.compile(r"://[^/\s:@]+:[^/\s]+@")

# 用户级的技能目录（相对导入根目录）；项目工程里另扫 .agents/skills、.pi/skills、.claude/skills
USER_SKILL_DIRS = ((".claude/skills", "claude"), (".cc-switch/skills", "cc-switch"),
                   (".pi/agent/skills", "pi"), (".agents/skills", "agents"))
PROJECT_SKILL_DIRS = ((".agents/skills", ""), (".pi/skills", "-pi"), (".claude/skills", "-claude"))


def _import_home(home=None):
    return home or os.environ.get("AGENT_DISPATCH_IMPORT_HOME") or os.path.expanduser("~")


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except ValueError as exc:
        raise DispatchError(f"{path} 不是合法的 JSON：{exc}")


def _write(path, data):
    store.write_text(path, json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _load(path):
    data = _read(path) or {}
    if not isinstance(data, dict):
        raise DispatchError(f"{path} 的格式不对，应该是「登记 ID: 定义」的对象")
    return data


def _save(path, reg_id, entry):
    if not REG_ID_RE.match(reg_id):
        raise DispatchError(f"登记 ID 只能包含字母数字和 . _ @ / + -：{reg_id!r}")
    data = _load(path)
    data[reg_id] = entry
    _write(path, data)


# ---------- 敏感信息遮罩 ----------

def _is_secret_flag(arg):
    """形如 --password / --api-key=… 的开关，它后面跟的值是敏感信息。"""
    key = arg.lstrip("-").split("=", 1)[0]
    return bool(key) and bool(SECRET_KEY_RE.search(key))


def mask_secret(text):
    """单个字符串：key=value 形式遮值，带凭据的 URL 整体遮掉，其余原样。"""
    if not isinstance(text, str):
        return text
    if CREDENTIAL_URL_RE.search(text):
        return MASK
    key, sep, value = text.partition("=")
    if sep and SECRET_KEY_RE.search(key):
        return f"{key}={MASK}" if value else text
    return text


def mask_env(env):
    out = {}
    for key, value in (env or {}).items():
        out[key] = MASK if ENV_SECRET_KEY_RE.search(key) else mask_secret(value)
    return out


def mask_any(value):
    """差异摘要等零散值：按同一套规则遮罩。"""
    if isinstance(value, str):
        return mask_secret(value)
    if isinstance(value, list):
        return [mask_any(v) for v in value]
    if isinstance(value, dict):
        return mask_env({str(k): str(v) for k, v in value.items()})
    return value


def mask_entry(entry):
    out = dict(entry)
    if entry.get("env"):
        out["env"] = mask_env(entry["env"])
    if entry.get("headers"):
        out["headers"] = {k: MASK for k in entry["headers"]}
    if entry.get("args"):
        masked = []
        for i, arg in enumerate(entry["args"]):
            previous = entry["args"][i - 1] if i else ""
            masked.append(MASK if _is_secret_flag(previous) else mask_secret(arg))
        out["args"] = masked
    if entry.get("url"):
        out["url"] = mask_secret(entry["url"])
    return out


def _merge_masked(original, submitted):
    """修改登记：提交值里等于遮罩占位（或遮罩后的 key=占位）的项保留磁盘上的原值。"""
    if not isinstance(original, dict):
        return submitted
    out = dict(submitted)
    for key in ("env", "headers"):
        if isinstance(submitted.get(key), dict) and isinstance(original.get(key), dict):
            out[key] = {k: original[key].get(k, v) if v == MASK else v for k, v in submitted[key].items()}
    if isinstance(submitted.get("args"), list) and isinstance(original.get("args"), list):
        merged = []
        for i, arg in enumerate(submitted["args"]):
            old = original["args"][i] if i < len(original["args"]) else None
            if arg == MASK:
                merged.append(old if old is not None else arg)
            elif arg.endswith("=" + MASK) and isinstance(old, str) and old.startswith(arg.split("=", 1)[0] + "="):
                merged.append(old)
            else:
                merged.append(arg)
        out["args"] = merged
    if submitted.get("url") == MASK:
        out["url"] = original.get("url")
    return out


# ---------- MCP 登记表 ----------

def load_mcp():
    return _load(MCP_REGISTRY)


def check_mcp_entry(entry):
    if not isinstance(entry, dict):
        raise DispatchError("MCP 条目必须是对象")
    etype = entry.get("type") or ("remote" if entry.get("url") else "local")
    if etype not in ("local", "remote"):
        raise DispatchError(f"type 只能是 local 或 remote，不是 {etype!r}")
    if etype == "local" and not entry.get("command"):
        raise DispatchError("local 类型的 MCP 服务要给 command（可执行命令）")
    if etype == "remote" and not entry.get("url"):
        raise DispatchError("remote 类型的 MCP 服务要给 url")
    if etype == "local" and entry.get("url"):
        raise DispatchError("local 类型用 command/args，不带 url")
    if etype == "remote" and entry.get("command"):
        raise DispatchError("remote 类型用 url/headers，不带 command")
    for key in ("env", "headers"):
        if entry.get(key) and (not isinstance(entry[key], dict)
                               or not all(isinstance(k, str) and isinstance(v, str) for k, v in entry[key].items())):
            raise DispatchError(f"{key} 必须是「键: 值」且都是字符串的对象")
    if entry.get("args") and (not isinstance(entry["args"], list)
                              or not all(isinstance(a, str) for a in entry["args"])):
        raise DispatchError("args 必须是字符串数组")
    if entry.get("exposure") and entry["exposure"] not in EXPOSURES:
        raise DispatchError(f"exposure 只能是 {' / '.join(EXPOSURES)}，不是 {entry['exposure']!r}")
    return etype


def save_mcp(reg_id, entry, source=None, project=None):
    """新增或修改一条 MCP 登记（键是登记 ID）。entry 里允许带遮罩占位（保留原值）。
    name 缺省等于登记 ID；来源保留已有的，没写过记为手动登记。project 是归属项目 ID（空 = 全局）。"""
    existing = load_mcp().get(reg_id, {})
    entry = _merge_masked(existing, dict(entry))
    etype = check_mcp_entry(entry)
    clean = {"name": entry.get("name") or existing.get("name") or reg_id, "type": etype,
             "sources": entry.get("sources") or existing.get("sources")
             or ([source] if source else [existing.get("source") or "手动登记"])}
    proj = project if project is not None else existing.get("project")
    if proj:
        clean["project"] = proj
    for key in ("command", "args", "env", "url", "headers", "description", "exposure"):
        if entry.get(key) not in (None, "", [], {}):
            clean[key] = entry[key]
    _save(MCP_REGISTRY, reg_id, clean)
    return clean


def entry_project(entry):
    return entry.get("project") or ""  # 没有 project 字段的旧数据视为全局


def entries_for(reg, project_id):
    """登记表里某个范围可用的条目：project_id 为空 = 只要全局；否则该项目的 + 全局的。"""
    return {reg_id: entry for reg_id, entry in reg.items() if entry_project(entry) in ("", project_id)}


def _users(kind, reg_id):
    """引用了这个登记 ID 的角色与项目默认：[{project: 项目ID（全局角色为空）, role: 角色ID}]。"""
    from . import definitions
    users = []
    for role in definitions.list_roles(None):
        if reg_id in (role.get(kind) or []):
            users.append({"project": "", "role": role["role"]})
    for project in definitions.list_projects():
        if reg_id in ((project.get("defaults") or {}).get(kind) or []):
            users.append({"project": project["id"], "role": "项目默认"})
        for role_id, data in definitions.project_role_layer(project["id"]).items():
            if reg_id in (data.get(kind) or []):
                users.append({"project": project["id"], "role": role_id})
    return users


def remove_mcp(reg_id):
    entry = load_mcp().get(reg_id)
    if entry is None:
        raise DispatchError(f"MCP 登记表里没有 {reg_id!r}")
    users = _users("mcp", reg_id)
    if users:
        where = "、".join(f"{u['project'] or '全局'}:{u['role']}" for u in users)
        raise DispatchError(f"MCP 服务 {reg_id}（name={entry.get('name') or reg_id}）还被这些角色引用：{where}。"
                            "先从这些角色的 mcp 字段里去掉再删除")
    data = load_mcp()
    data.pop(reg_id, None)
    _write(MCP_REGISTRY, data)


# ---------- 技能登记表 ----------

def load_skills():
    return _load(SKILLS_REGISTRY)


def skill_description(directory):
    """技能目录里 SKILL.md 头部 frontmatter 的 description。"""
    text = store.read_text(os.path.join(directory, "SKILL.md"), "") or ""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        for line in text[4:end if end > 0 else 0].splitlines():
            if line.startswith("description:"):
                return line.split(":", 1)[1].strip().strip("\"'")
    return ""


def save_skill(reg_id, path, name=None, source=None, project=None):
    """登记一个技能目录（键是登记 ID），name 缺省取目录名。project 是归属项目 ID（空 = 全局）。"""
    directory = os.path.realpath(os.path.expanduser(path))
    if not os.path.isdir(directory) or not os.path.isfile(os.path.join(directory, "SKILL.md")):
        raise DispatchError(f"目录里没有 SKILL.md，不算技能：{directory}")
    existing = load_skills().get(reg_id, {})
    entry = {"name": name or existing.get("name") or os.path.basename(directory.rstrip("/")) or reg_id,
             "path": directory,
             "paths": sorted({directory, *(existing.get("paths") or [])}),
             "description": skill_description(directory),
             "sources": existing.get("sources") or ([source] if source else ["手动登记"])}
    proj = project if project is not None else existing.get("project")
    if proj:
        entry["project"] = proj
    _save(SKILLS_REGISTRY, reg_id, entry)
    return entry


def remove_skill(reg_id):
    entry = load_skills().get(reg_id)
    if entry is None:
        raise DispatchError(f"技能登记表里没有 {reg_id!r}")
    users = _users("skills", reg_id)
    if users:
        where = "、".join(f"{u['project'] or '全局'}:{u['role']}" for u in users)
        raise DispatchError(f"技能 {reg_id}（name={entry.get('name') or reg_id}）还被这些角色引用：{where}。"
                            "先从这些角色的 skills 字段里去掉再删除")
    data = load_skills()
    data.pop(reg_id, None)
    _write(SKILLS_REGISTRY, data)


# ---------- 从现有配置找候选（按服务名 / 技能名分组） ----------

def _normalize_mcp(raw, label):
    """pi 与 OpenCode 两种写法统一成登记表结构；不完整（仅 enabled 覆盖）或停用的条目不带进来。"""
    if not isinstance(raw, dict) or (raw.get("enabled") is False):
        return None
    if raw.get("command") is None and raw.get("url") is None:
        return None
    entry = {"type": "remote" if raw.get("url") else "local"}
    if isinstance(raw.get("command"), list):  # OpenCode：command 是数组
        command = [str(c) for c in raw["command"]]
        entry["command"], entry["args"] = (command[0], command[1:]) if command else ("", [])
    else:
        entry["command"], entry["args"] = str(raw.get("command") or ""), [str(a) for a in raw.get("args") or []]
    if raw.get("url"):
        entry["url"] = str(raw["url"])
    if isinstance(raw.get("env"), dict):
        entry["env"] = {str(k): str(v) for k, v in raw["env"].items()}
    if isinstance(raw.get("headers"), dict):
        entry["headers"] = {str(k): str(v) for k, v in raw["headers"].items()}
    if raw.get("exposure") in EXPOSURES:
        entry["exposure"] = raw["exposure"]
    entry["source"] = label
    return entry


def _projects():
    from . import definitions
    try:
        return definitions.list_projects()
    except DispatchError:
        return []


def _mcp_sources(home, project=None):
    """[{label, short, path, project}]：short 用于生成登记 ID（<服务名>@<来源简称>），
    project 是来源归属的项目 ID（空 = 用户全局配置）。project 给了就只看该项目的工程来源 + 全局来源。"""
    sources = [{"label": "pi 全局 ~/.pi/agent/mcp.json", "short": "pi-global", "project": "",
                "path": os.path.join(home, ".pi", "agent", "mcp.json")},
               {"label": "OpenCode 全局 ~/.config/opencode/opencode.json", "short": "oc-global", "project": "",
                "path": os.path.join(home, ".config", "opencode", "opencode.json")}]
    for proj in _projects():
        if project is not None and project != proj["id"]:
            continue
        for repo_id, repo in proj["repos"].items():
            short = f"{proj['id']}-{repo_id}"
            sources += [{"label": f"项目 {proj['id']}/{repo_id} 的 .pi/mcp.json", "short": short, "project": proj["id"],
                         "path": os.path.join(repo["cwd"], ".pi", "mcp.json")},
                        {"label": f"项目 {proj['id']}/{repo_id} 的 opencode.json", "short": short + "-oc", "project": proj["id"],
                         "path": os.path.join(repo["cwd"], "opencode.json")}]
    return sources


def _identity(entry):
    """判断两条是不是同一个服务：不看来源、备注、下发用的 name 和归属项目。"""
    return json.dumps({k: v for k, v in entry.items() if k not in ("source", "sources", "description", "name", "project")},
                      ensure_ascii=False, sort_keys=True)


def _free_id(registry, base, identity, name, scope=None):
    """登记 ID 被内容不同的条目占了时加 -2、-3 后缀。scope 限定哪些归属的条目算「已登记且相同」：
    None = 任意归属；"" = 只要全局；项目 ID = 该项目的 + 全局的。"""
    reg_id = base
    counter = 1
    while reg_id in registry:
        existing = registry[reg_id]
        in_scope = scope is None or entry_project(existing) in ("", scope) if scope != "" else entry_project(existing) == ""
        if in_scope and _identity(existing) == identity and (existing.get("name") or reg_id) == name:
            return reg_id, True  # 已登记且相同
        counter += 1
        reg_id = f"{base}-{counter}"
    return reg_id, False


def mcp_candidates(home=None, project=None):
    """现有配置里的 MCP 候选，按服务名分组：[{name, conflict, variants: [...]}]。
    project 为 None 看全部来源（兼容旧用法），"" 只看全局来源，项目 ID 看该项目工程来源 + 全局来源；
    每个变体带 project（归属项目 ID，取第一个来源的）与 diff（这组里取值不一样的参数）。"""
    home = _import_home(home)
    groups = {}
    for src in _mcp_sources(home, project):
        data = _read(src["path"])
        if data is None:
            continue
        servers = data.get("mcpServers") if isinstance(data, dict) else None
        servers = servers if isinstance(servers, dict) else (data.get("mcp") if isinstance(data, dict) else None)
        if not isinstance(servers, dict):
            continue
        for svc_name, raw in servers.items():
            entry = _normalize_mcp(raw, src["label"])
            if entry:
                groups.setdefault(str(svc_name), {}).setdefault(_identity(entry), {"entry": entry, "sources": []})["sources"].append(src)
    registry = load_mcp()
    out = []
    for name in sorted(groups):
        clusters = groups[name]
        variants = []
        keys = set()
        for cluster in clusters.values():
            keys |= set(cluster["entry"])
        diff_keys = [k for k in sorted(keys) if len({json.dumps(c["entry"].get(k), ensure_ascii=False, sort_keys=True)
                                                     for c in clusters.values()}) > 1]
        for identity, cluster in clusters.items():
            primary = cluster["sources"][0]
            base = name if len(clusters) == 1 else f"{name}@{primary['short']}"
            reg_id, registered = _free_id(registry, base, identity, name, project)
            variants.append({"id": reg_id, "name": name, "entry": cluster["entry"],
                             "source": primary["label"], "sources": [s["label"] for s in cluster["sources"]],
                             "project": primary["project"],
                             "projects": sorted({s["project"] for s in cluster["sources"]}),
                             "registered": registered,
                             "diff": {k: cluster["entry"].get(k) for k in diff_keys}})
        variants.sort(key=lambda v: v["source"])
        out.append({"name": name, "conflict": len(clusters) > 1, "variants": variants})
    return out


def _pick_mcp_variant(selection, candidates):
    """把一个导入选择（登记 ID / 服务名 / {name, source}）解析成一个变体。"""
    ref, source = (selection, None) if isinstance(selection, str) else (selection.get("name"), selection.get("source"))
    if not ref:
        raise DispatchError("导入选择要有 name（服务名或登记 ID）")
    matches = []
    for group in candidates:
        for variant in group["variants"]:
            if (variant["id"] == ref or group["name"] == ref) and (source is None or source in variant["sources"]):
                matches.append((group, variant))
    if not matches:
        raise DispatchError(f"没有找到 MCP 服务候选 {ref!r}。先看：agent-dispatch mcp import（不带参数列出候选）")
    distinct = []
    for group, variant in matches:  # 同名（或同 ID 前缀）多个不同内容的变体只留一个
        if not any(v is variant or _identity(v["entry"]) == _identity(variant["entry"]) for _, v in distinct):
            distinct.append((group, variant))
    if len(distinct) > 1:
        listing = "；".join(f"{v['id']}（{v['source']}）：{_describe(v['entry'])}" for _, v in distinct)
        raise DispatchError(f"MCP 服务 {ref} 有多个内容不同的候选，请按登记 ID 或来源指明"
                            f"（import <登记ID> 或 --source <来源>）。{listing}")
    return distinct[0]


def import_mcp(selections, home=None, project=None):
    """导入选中的候选。选择可以是登记 ID、服务名（不冲突时直接用）或 {name, source}。
    project 限定候选范围；归属按变体自己的来源定（工程里扫出来的归该项目，全局来源归全局）。
    返回真正新登记的登记 ID；已登记且内容相同的跳过。"""
    home = _import_home(home)
    candidates = mcp_candidates(home, project)
    added = []
    for selection in selections:
        group, variant = _pick_mcp_variant(selection, candidates)
        registry = load_mcp()
        reg_id, already = _free_id(registry, variant["id"], _identity(variant["entry"]), group["name"], project)
        if already:
            continue
        entry = dict(variant["entry"])
        entry.pop("source", None)
        clean = {**entry, "name": group["name"], "sources": variant["sources"]}
        if variant.get("project"):
            clean["project"] = variant["project"]
        _save(MCP_REGISTRY, reg_id, clean)
        added.append(reg_id)
    return added


def _describe(entry):
    if entry.get("url"):
        return entry["url"]
    return " ".join([entry.get("command") or ""] + (entry.get("args") or []))


# ---------- 技能候选与导入 ----------

def _skill_sources(home, project=None):
    """与 _mcp_sources 同一套规则：project 给了就只看该项目工程 + 用户全局目录。"""
    sources = [{"label": f"~/{rel}", "short": short, "project": "", "path": os.path.join(home, *rel.split("/"))}
               for rel, short in USER_SKILL_DIRS]
    for proj in _projects():
        if project is not None and project != proj["id"]:
            continue
        for repo_id, repo in proj["repos"].items():
            short = f"{proj['id']}-{repo_id}"
            for rel, suffix in PROJECT_SKILL_DIRS:
                sources.append({"label": f"项目 {proj['id']}/{repo_id} 的 {rel}", "short": short + suffix,
                                "project": proj["id"], "path": os.path.join(repo["cwd"], *rel.split("/"))})
    return sources


def _skill_digest(path):
    with open(os.path.join(path, "SKILL.md"), "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def skill_candidates(home=None, project=None):
    """各处技能目录里的候选，按技能名分组：[{name, conflict, variants: [...]}]。
    先按真实路径去重（软链接不算重复），再按 SKILL.md 内容合并：内容相同的目录合成一个变体
    （记下全部来源，取第一个目录作默认路径）；内容不同的各自成变体。project 限定来源范围，
    语义与 mcp_candidates 相同。"""
    home = _import_home(home)
    groups = {}
    for src in _skill_sources(home, project):
        if not os.path.isdir(src["path"]):
            continue
        for sub in sorted(os.listdir(src["path"])):
            path = os.path.realpath(os.path.join(src["path"], sub))
            if not os.path.isdir(path) or not os.path.isfile(os.path.join(path, "SKILL.md")):
                continue
            cluster = groups.setdefault(sub, {}).setdefault(_skill_digest(path), {"paths": [], "sources": []})
            if path not in cluster["paths"]:  # realpath 去重：软链接和同一目录不重复计
                cluster["paths"].append(path)
                cluster["sources"].append(src)
    registry = load_skills()
    out = []
    for name in sorted(groups):
        variants = []
        for digest, cluster in groups[name].items():
            primary = cluster["sources"][0]
            base = name if len(groups[name]) == 1 else f"{name}@{primary['short']}"
            reg_id = base
            counter = 1
            registered = False
            while reg_id in registry:
                entry = registry[reg_id]
                in_scope = (entry_project(entry) == project if project == ""
                            else project is None or entry_project(entry) in ("", project))
                if in_scope and (entry.get("name") or reg_id) == name and entry.get("path") in cluster["paths"]:
                    registered = True
                    break
                counter += 1
                reg_id = f"{base}-{counter}"
            variants.append({"id": reg_id, "name": name, "path": cluster["paths"][0], "paths": cluster["paths"],
                             "description": skill_description(cluster["paths"][0]),
                             "source": primary["label"], "sources": [s["label"] for s in cluster["sources"]],
                             "project": primary["project"],
                             "projects": sorted({s["project"] for s in cluster["sources"]}),
                             "registered": registered})
        variants.sort(key=lambda v: v["source"])
        out.append({"name": name, "conflict": len(groups[name]) > 1, "variants": variants})
    return out


def import_skills(selections, home=None, project=None):
    home = _import_home(home)
    candidates = skill_candidates(home, project)
    added = []
    for selection in selections:
        ref, source = (selection, None) if isinstance(selection, str) else (selection.get("name"), selection.get("source"))
        if not ref:
            raise DispatchError("导入选择要有 name（技能名或登记 ID）")
        matches = []
        for group in candidates:
            for variant in group["variants"]:
                if (variant["id"] == ref or group["name"] == ref) and (source is None or source in variant["sources"]):
                    matches.append((group, variant))
        if not matches:
            raise DispatchError(f"没有找到技能候选 {ref!r}。先看：agent-dispatch skills import（不带参数列出候选）")
        if len(matches) > 1:
            listing = "；".join(f"{v['id']}（{v['source']}）：{v['path']}" for _, v in matches)
            raise DispatchError(f"技能 {ref} 有多个内容不同的候选，请按登记 ID 或来源指明。{listing}")
        group, variant = matches[0]
        if variant["registered"]:
            continue
        entry = {"name": group["name"], "path": variant["path"], "paths": variant["paths"],
                 "description": variant["description"], "sources": variant["sources"]}
        if variant.get("project"):
            entry["project"] = variant["project"]
        reg_id = variant["id"]
        registry = load_skills()
        counter = 1
        while reg_id in registry:  # ID 被别的技能占了（手动登记过）就加后缀
            counter += 1
            reg_id = f"{variant['id']}-{counter}"
        _save(SKILLS_REGISTRY, reg_id, entry)
        added.append(reg_id)
    return added


def used_by(kind, reg_id):
    """给网页用：这个登记 ID 被哪些角色使用。"""
    return _users(kind, reg_id)
