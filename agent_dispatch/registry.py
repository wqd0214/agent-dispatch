"""MCP 服务与技能的登记表（全局，一处登记），以及从现有 pi / OpenCode 配置导入。

登记表：
  ~/.agent-dispatch/mcp.json    {名称: {type: "local"|"remote", command, args, env, url, headers, description, source}}
  ~/.agent-dispatch/skills.json {名称: {path, description, source}}

敏感信息：env 的值、headers 的值、args / url 里形如密码令牌的内容只存本机文件，
对外（命令行输出、网页接口）一律用 MASK 遮罩；修改登记时提交的值若等于遮罩占位，则保留原值。

导入只读来源文件，绝不修改它们。来源根目录可用 AGENT_DISPATCH_IMPORT_HOME 指到别处（测试用）。
"""
import json
import os
import re

from . import store
from .store import DispatchError

MCP_REGISTRY = os.path.join(store.HOME, "mcp.json")
SKILLS_REGISTRY = os.path.join(store.HOME, "skills.json")
MASK = "••••"

# 看起来像密码 / 令牌的参数：key=value 形式按 key 判断，URL 带凭据的整体遮掉
SECRET_KEY_RE = re.compile(r"(?i)(password|passwd|token|secret|api[_-]?key|authorization)")
CREDENTIAL_URL_RE = re.compile(r"://[^/\s:@]+:[^/\s]+@")

# 用户级的技能目录（相对导入根目录）；项目工程里另扫 .agents/skills、.pi/skills、.claude/skills
USER_SKILL_DIRS = (".claude/skills", ".cc-switch/skills", ".pi/agent/skills", ".agents/skills")
PROJECT_SKILL_DIRS = (".agents/skills", ".pi/skills", ".claude/skills")


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
        raise DispatchError(f"{path} 的格式不对，应该是「名称: 定义」的对象")
    return data


def _save(path, name, entry):
    store.check_name(name)  # 名称会进各种路径与参数，先挡住怪字符
    data = _load(path)
    data[name] = entry
    _write(path, data)


# ---------- 敏感信息遮罩 ----------

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


def _is_secret_flag(arg):
    """形如 --password / --api-key=… 的开关，它后面跟的值是敏感信息。"""
    key = arg.lstrip("-").split("=", 1)[0]
    return bool(key) and bool(SECRET_KEY_RE.search(key))


def mask_entry(entry):
    out = dict(entry)
    if entry.get("env"):
        out["env"] = {k: MASK for k in entry["env"]}
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
    return etype


def save_mcp(name, entry, source=None):
    """新增或修改一条 MCP 登记。entry 里允许带遮罩占位（保留原值）。"""
    entry = _merge_masked(load_mcp().get(name), dict(entry))
    etype = check_mcp_entry(entry)
    known = {"type", "command", "args", "env", "url", "headers", "description"}
    clean = {"type": etype, "source": source or load_mcp().get(name, {}).get("source") or "手动登记"}
    for key in known:
        if entry.get(key) not in (None, "", [], {}):
            clean[key] = entry[key]
    _save(MCP_REGISTRY, name, clean)
    return clean


def _users(kind, name):
    """引用了这个名称的角色：[{project: 项目ID（全局角色为空）, role: 角色ID}]。"""
    from . import definitions
    users = []
    for role in definitions.list_roles(None):
        if name in (role.get(kind) or []):
            users.append({"project": "", "role": role["role"]})
    for project in definitions.list_projects():
        for role in definitions.list_roles(project):
            if name in (role.get(kind) or []):
                users.append({"project": project["id"], "role": role["role"]})
    return users


def remove_mcp(name):
    users = _users("mcp", name)
    if users:
        where = "、".join(f"{u['project'] or '全局'}:{u['role']}" for u in users)
        raise DispatchError(f"MCP 服务 {name} 还被这些角色引用：{where}。先从这些角色的 mcp 字段里去掉再删除")
    data = load_mcp()
    if data.pop(name, None) is None:
        raise DispatchError(f"MCP 登记表里没有 {name!r}")
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


def save_skill(name, path, source=None):
    directory = os.path.realpath(os.path.expanduser(path))
    if not os.path.isdir(directory) or not os.path.isfile(os.path.join(directory, "SKILL.md")):
        raise DispatchError(f"目录里没有 SKILL.md，不算技能：{directory}")
    _save(SKILLS_REGISTRY, name, {"path": directory, "description": skill_description(directory),
                                  "source": source or "手动登记"})


def remove_skill(name):
    users = _users("skills", name)
    if users:
        where = "、".join(f"{u['project'] or '全局'}:{u['role']}" for u in users)
        raise DispatchError(f"技能 {name} 还被这些角色引用：{where}。先从这些角色的 skills 字段里去掉再删除")
    data = load_skills()
    if data.pop(name, None) is None:
        raise DispatchError(f"技能登记表里没有 {name!r}")
    _write(SKILLS_REGISTRY, data)


# ---------- 从现有配置找候选 ----------

def _normalize_mcp(raw, source):
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
    entry["source"] = source
    return entry


def _mcp_sources(home):
    """[(来源标签, 文件路径)]：用户级 pi / OpenCode，加上各项目工程里的 .pi/mcp.json 和 opencode.json。"""
    from . import definitions
    sources = [("pi 全局 ~/.pi/agent/mcp.json", os.path.join(home, ".pi", "agent", "mcp.json")),
               ("OpenCode 全局 ~/.config/opencode/opencode.json",
                os.path.join(home, ".config", "opencode", "opencode.json"))]
    try:
        projects = definitions.list_projects()
    except DispatchError:
        projects = []
    for project in projects:
        for repo_id, repo in project["repos"].items():
            sources += [(f"项目 {project['id']}/{repo_id} 的 .pi/mcp.json", os.path.join(repo["cwd"], ".pi", "mcp.json")),
                        (f"项目 {project['id']}/{repo_id} 的 opencode.json", os.path.join(repo["cwd"], "opencode.json"))]
    return sources


def _identity(entry):
    """判断两条是不是同一个服务：不看来源与备注。"""
    return json.dumps({k: v for k, v in entry.items() if k not in ("source", "description")},
                      ensure_ascii=False, sort_keys=True)


def mcp_candidates(home=None):
    """现有配置里的 MCP 服务候选：[{name, entry, source, registered, conflict}]。
    同名内容不同的（conflict）都列出，导入时用来源消歧。"""
    home = _import_home(home)
    groups = {}
    for label, path in _mcp_sources(home):
        data = _read(path)
        if data is None:
            continue
        servers = data.get("mcpServers") if isinstance(data, dict) else None
        servers = servers if isinstance(servers, dict) else (data.get("mcp") if isinstance(data, dict) else None)
        if not isinstance(servers, dict):
            continue
        for name, raw in servers.items():
            entry = _normalize_mcp(raw, label)
            if entry:
                groups.setdefault(str(name), []).append(entry)
    registry = load_mcp()
    out = []
    for name in sorted(groups):
        conflict = len({_identity(e) for e in groups[name]}) > 1
        for entry in groups[name]:
            registered = _identity(registry.get(name, {})) == _identity(entry)
            out.append({"name": name, "entry": entry, "source": entry["source"],
                        "registered": registered, "conflict": conflict})
    return out


def import_mcp(selections, home=None):
    """导入选中的候选。selections 是名称的列表，或 {name, source} 的列表（同名冲突时用来源消歧）。
    返回真正新登记的名称；已登记且内容相同的跳过。"""
    home = _import_home(home)
    added = []
    for selection in selections:
        name, source = (selection, None) if isinstance(selection, str) else (selection.get("name"), selection.get("source"))
        variants = [c for c in mcp_candidates(home) if c["name"] == name]
        if not variants:
            raise DispatchError(f"没有找到 MCP 服务候选 {name!r}。先看：agent-dispatch mcp import（不带参数列出候选）")
        if source:
            variants = [v for v in variants if v["source"] == source]
            if not variants:
                raise DispatchError(f"候选 {name!r} 没有来自「{source}」的版本")
        distinct = []
        for v in variants:  # 同一来源文件里同名只留一个
            if _identity(v["entry"]) not in {_identity(d["entry"]) for d in distinct}:
                distinct.append(v)
        if len(distinct) > 1:
            listing = "；".join(f"{v['source']}：{_describe(v['entry'])}" for v in distinct)
            raise DispatchError(f"MCP 服务 {name} 在多处配置且内容不同，请指明来源（import {name} --source <来源>）。{listing}")
        entry = distinct[0]["entry"]
        if name in load_mcp() and _identity(load_mcp()[name]) == _identity(entry):
            continue  # 已登记且相同
        _save(MCP_REGISTRY, name, entry)
        added.append(name)
    return added


def _describe(entry):
    if entry.get("url"):
        return entry["url"]
    return " ".join([entry.get("command") or ""] + (entry.get("args") or []))


# ---------- 技能候选与导入 ----------

def _skill_sources(home):
    from . import definitions
    sources = [(f"~/{rel}", os.path.join(home, *rel.split("/"))) for rel in USER_SKILL_DIRS]
    try:
        projects = definitions.list_projects()
    except DispatchError:
        projects = []
    for project in projects:
        for repo_id, repo in project["repos"].items():
            sources += [(f"项目 {project['id']}/{repo_id} 的 {rel}", os.path.join(repo["cwd"], *rel.split("/")))
                        for rel in PROJECT_SKILL_DIRS]
    return sources


def skill_candidates(home=None):
    """各处技能目录里的候选：目录里有 SKILL.md 才算。"""
    home = _import_home(home)
    registry = load_skills()
    groups = {}
    for label, directory in _skill_sources(home):
        if not os.path.isdir(directory):
            continue
        for sub in sorted(os.listdir(directory)):
            path = os.path.join(directory, sub)
            if os.path.isdir(path) and os.path.isfile(os.path.join(path, "SKILL.md")):
                groups.setdefault(sub, []).append((os.path.realpath(path), label))
    out = []
    for name in sorted(groups):
        paths = {p for p, _ in groups[name]}
        for path, label in groups[name]:
            registered = registry.get(name, {}).get("path") == path
            out.append({"name": name, "path": path, "description": skill_description(path),
                        "source": label, "registered": registered, "conflict": len(paths) > 1})
    return out


def import_skills(selections, home=None):
    home = _import_home(home)
    added = []
    for selection in selections:
        name, source = (selection, None) if isinstance(selection, str) else (selection.get("name"), selection.get("source"))
        variants = [c for c in skill_candidates(home) if c["name"] == name]
        if not variants:
            raise DispatchError(f"没有找到技能候选 {name!r}。先看：agent-dispatch skills import（不带参数列出候选）")
        if source:
            variants = [v for v in variants if v["source"] == source]
            if not variants:
                raise DispatchError(f"技能候选 {name!r} 没有来自「{source}」的版本")
        if len({v["path"] for v in variants}) > 1:
            listing = "；".join(f"{v['source']}：{v['path']}" for v in variants)
            raise DispatchError(f"技能 {name} 在多处目录出现，请指明来源（import {name} --source <来源>）。{listing}")
        picked = variants[0]
        if load_skills().get(name, {}).get("path") == picked["path"]:
            continue
        save_skill(name, picked["path"], picked["source"])
        added.append(name)
    return added


def used_by(kind, name):
    """给网页用：这个 MCP / 技能被哪些角色使用。"""
    return _users(kind, name)
