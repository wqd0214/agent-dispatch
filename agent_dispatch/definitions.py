"""项目、工程与角色的定义。

项目：一条产品线或一个工程群（如 C2），下面有多个工程（前端、后端、测试……）。
工程：项目里的一个代码仓库目录，可以带自己的资源（如浏览器通道）。
角色：项目里的一个岗位（后端开发工程师、测试工程师……）——在哪个工程干活、用哪个后端和
      运行方式、派工单怎么写、过哪些闸门。

目录布局：
  ~/.agent-dispatch/projects/<项目ID>/project.json        项目定义（含工程清单）
  ~/.agent-dispatch/projects/<项目ID>/supervisor.md       给主管的项目专属规则（可选）
  ~/.agent-dispatch/projects/<项目ID>/roles/<角色ID>.json  项目的角色，或对全局同名角色的覆盖
  ~/.agent-dispatch/roles/<角色ID>.json                    本机的全局角色
  <agent-dispatch 工程>/roles/<角色ID>.json                随工具发布的全局角色

角色的派工单模板是同目录下的 <角色ID>.brief.md（或在 json 里用 brief_template 指定）。
解析顺序：全局角色 → 项目的 defaults → 项目角色，后者逐项覆盖前者。
"""
import json
import os

from . import backends, store
from .store import DispatchError

PROJECTS_DIR = os.path.join(store.HOME, "projects")
LOCAL_ROLE_DIR = os.path.join(store.HOME, "roles")         # 本机的全局角色，或对随工具发布的角色的覆盖
SHIPPED_ROLE_DIR = os.path.join(backends.ROOT, "roles")    # 随工具发布的全局角色
GLOBAL_ROLE_DIRS = [LOCAL_ROLE_DIR, SHIPPED_ROLE_DIR]


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except ValueError as exc:
        raise DispatchError(f"{path} 不是合法的 JSON：{exc}")


def _write_json(path, data):
    store.write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def _expand(path):
    return os.path.realpath(os.path.expanduser(os.path.expandvars(path)))


# ---------- 项目与工程 ----------

def project_dir(project_id):
    return os.path.join(PROJECTS_DIR, project_id)


def _project_file(project_id):
    return os.path.join(project_dir(project_id), "project.json")


def load_project(ref):
    """按 ID 或显示名找项目。"""
    data = _read_json(_project_file(ref)) if store.NAME_RE.match(ref) else None
    project_id = ref
    if data is None:
        for candidate in _project_ids():
            other = _read_json(_project_file(candidate)) or {}
            if other.get("name") == ref:
                data, project_id = other, candidate
                break
    if data is None:
        known = "、".join(_project_ids()) or "（还没有定义任何项目）"
        raise DispatchError(f"没有定义项目 {ref!r}。已定义：{known}。新建：agent-dispatch projects add <ID> --repo <工程名>=<目录>")
    repos = {}
    for repo_id, repo in (data.get("repos") or {}).items():
        cwd = _expand(repo["cwd"])
        repos[repo_id] = {**repo, "id": repo_id, "cwd": cwd, "name": repo.get("name") or os.path.basename(cwd),
                          "channels": repo.get("channels") or [],
                          "channel_ns": repo.get("channel_namespace") or f"{project_id}.{repo_id}"}
    return {**data, "id": project_id, "name": data.get("name") or project_id, "repos": repos}


def _project_ids():
    if not os.path.isdir(PROJECTS_DIR):
        return []
    return [p for p in sorted(os.listdir(PROJECTS_DIR)) if os.path.isfile(_project_file(p))]


def list_projects():
    projects = []
    for project_id in _project_ids():
        try:
            projects.append(load_project(project_id))
        except (DispatchError, KeyError):
            continue
    return projects


def detect(path):
    """按目录找 (项目, 工程ID)：取工程目录是该路径前缀的里面最深的那个（worktree 归到主工程）。"""
    path = _expand(path).split("/.worktrees/")[0]
    best = (None, None, -1)
    for project in list_projects():
        for repo_id, repo in project["repos"].items():
            if (path == repo["cwd"] or path.startswith(repo["cwd"] + os.sep)) and len(repo["cwd"]) > best[2]:
                best = (project, repo_id, len(repo["cwd"]))
    return best[0], best[1]


def supervisor_notes(project):
    return store.read_text(os.path.join(project_dir(project["id"]), "supervisor.md"))


def add_project(project_id, name=None, repos=None):
    store.check_name(project_id)
    if os.path.exists(_project_file(project_id)):
        raise DispatchError(f"项目 {project_id} 已存在：{_project_file(project_id)}")
    _write_json(_project_file(project_id), {"name": name or project_id, "repos": {}})
    for repo_id, cwd in (repos or {}).items():
        add_repo(project_id, repo_id, cwd)
    return _project_file(project_id)


def add_repo(project_id, repo_id, cwd, channels=None, channel_namespace=None):
    store.check_name(repo_id)
    data = _read_json(_project_file(project_id))
    if data is None:
        raise DispatchError(f"没有定义项目 {project_id!r}")
    cwd = _expand(cwd)
    if not os.path.isdir(cwd):
        raise DispatchError(f"目录不存在：{cwd}")
    repo = {"cwd": cwd}
    if channels:
        repo["channels"] = channels
    if channel_namespace:
        repo["channel_namespace"] = channel_namespace
    data.setdefault("repos", {})[repo_id] = {**(data.get("repos") or {}).get(repo_id, {}), **repo}
    _write_json(_project_file(project_id), data)


def update_project(project_id, name=None, default_role=None, defaults=None):
    """改项目定义：显示名、默认角色、defaults 覆盖层。只改给定的项。"""
    data = _read_json(_project_file(project_id))
    if data is None:
        raise DispatchError(f"没有定义项目 {project_id!r}")
    if name is not None:
        data["name"] = name
    if default_role is not None:
        find_role_id(load_project(project_id), default_role)  # 角色不存在就拒
        data["default_role"] = default_role
    if defaults is not None:
        # 值为 None 表示删掉这个字段（恢复未管理）；其余按正常校验后合并
        removals = [k for k, v in defaults.items() if v is None]
        checked = {k: v for k, v in defaults.items() if v is not None}
        _check_field_values(load_project(project_id), checked)
        merged = {**(data.get("defaults") or {}), **checked}
        for key in removals:
            merged.pop(key, None)
        if merged:
            data["defaults"] = merged
        else:
            data.pop("defaults", None)
    _write_json(_project_file(project_id), data)
    return _project_file(project_id)


def set_project_enabled(project_id, key, reg_id, enabled):
    """调整项目 defaults 里 mcp / skills 启用清单的一项（写入时字段不存在就新建，即开始管理）。"""
    if key not in ("mcp", "skills"):
        raise DispatchError(f"只能调整 mcp / skills，不是 {key!r}")
    from . import registry
    project = load_project(project_id)
    reg = registry.load_mcp() if key == "mcp" else registry.load_skills()
    if reg_id not in registry.entries_for(reg, project_id):
        raise DispatchError(f"{reg_id} 不在项目 {project_id} 可用的登记条目里"
                            f"（本项目的 + 全局的）；它归属项目 {registry.entry_project(reg.get(reg_id) or {}) or '未知'}")
    current = list((project.get("defaults") or {}).get(key) or [])
    updated = [i for i in current if i != reg_id] + ([reg_id] if enabled else [])
    update_project(project_id, defaults={key: updated})


def remove_repo(project_id, repo_id):
    """从项目里删工程；还有角色的 repos 引用它时拒绝。"""
    project = load_project(project_id)
    if repo_id not in project["repos"]:
        raise DispatchError(f"项目 {project_id} 没有工程 {repo_id!r}，可选：{'、'.join(project['repos'])}")
    referencing = [r["role"] for r in list_roles(project) if repo_id in (r.get("repos") or [])]
    if referencing:
        raise DispatchError(f"工程 {repo_id} 还被角色 {'、'.join(referencing)} 引用，先改这些角色的 repos 再删除")
    data = _read_json(_project_file(project_id))
    data.get("repos", {}).pop(repo_id, None)
    _write_json(_project_file(project_id), data)


def project_role_layer(project_id):
    """项目角色层的原始 json（未与全局 / defaults 合并），给引用检查这类逐层判断用。"""
    directory = os.path.join(project_dir(project_id), "roles")
    out = {}
    if os.path.isdir(directory):
        for filename in sorted(os.listdir(directory)):
            if filename.endswith(".json"):
                data = _read_json(os.path.join(directory, filename))
                if data is not None:
                    out[filename[:-5]] = data
    return out


def get_supervisor_notes(project_id):
    return store.read_text(os.path.join(project_dir(project_id), "supervisor.md"))


def set_supervisor_notes(project_id, text):
    if _read_json(_project_file(project_id)) is None:
        raise DispatchError(f"没有定义项目 {project_id!r}")
    path = os.path.join(project_dir(project_id), "supervisor.md")
    store.write_text(path, text)
    return path


# ---------- 角色 ----------

def _project_role_dirs(project):
    return [os.path.join(project_dir(project["id"]), "roles")] if project else []


def _find_role(dirs, role_id):
    """在一组目录里找角色定义，返回 (定义, 派工单模板路径)。"""
    for directory in dirs:
        data = _read_json(os.path.join(directory, f"{role_id}.json"))
        if data is None:
            continue
        brief = os.path.join(directory, data.get("brief_template") or f"{role_id}.brief.md")
        return data, (brief if os.path.isfile(brief) else None)
    return None, None


def _role_ids(dirs):
    ids = []
    for directory in dirs:
        if os.path.isdir(directory):
            ids += [f[:-5] for f in sorted(os.listdir(directory)) if f.endswith(".json") and f[:-5] not in ids]
    return ids


def _global_role(role_id):
    """全局角色 = 随工具发布的定义，再叠加本机 ~/.agent-dispatch/roles 里的同名覆盖。"""
    shipped, shipped_brief = _find_role([SHIPPED_ROLE_DIR], role_id)
    local, local_brief = _find_role([LOCAL_ROLE_DIR], role_id)
    if shipped is None and local is None:
        return None, None
    return {**(shipped or {}), **(local or {})}, local_brief or shipped_brief


def _merged_role(project, role_id):
    global_role, global_brief = _global_role(role_id)
    project_role, project_brief = _find_role(_project_role_dirs(project), role_id)
    if global_role is None and project_role is None:
        return None
    merged = {**(global_role or {}), **((project or {}).get("defaults") or {}), **(project_role or {})}
    merged.pop("brief_template", None)
    repos = merged.pop("repos", None) or ([merged["repo"]] if merged.get("repo") else [])
    merged.pop("repo", None)
    pool_source = "项目" if (project_role or {}).get("model_pool") else ("全局" if (global_role or {}).get("model_pool") else None)
    merged.update(role=role_id, title=merged.get("title") or role_id, repos=repos, pool_source=pool_source,
                  _brief_path=project_brief or global_brief,
                  scope="全局" if project_role is None else ("项目覆盖全局" if global_role else "项目专属"))
    return merged


def find_role_id(project, ref):
    """角色可以用 ID 或中文称呼（title）来指。"""
    ids = _role_ids(_project_role_dirs(project) + GLOBAL_ROLE_DIRS)
    if ref in ids:
        return ref
    for role_id in ids:
        if (_merged_role(project, role_id) or {}).get("title") == ref:
            return role_id
    where = f"项目 {project['id']} 和全局" if project else "全局"
    listing = "、".join(f"{r['role']}（{r['title']}）" for r in list_roles(project)) or "（无）"
    raise DispatchError(f"{where}都没有角色 {ref!r}。可用：{listing}")


def list_roles(project=None):
    """项目可用的全部角色：项目自己的在前，其次是全局的。"""
    return [_merged_role(project, role_id) for role_id in _role_ids(_project_role_dirs(project) + GLOBAL_ROLE_DIRS)]


def resolve(project, role_ref, repo_id=None, detected_repo=None):
    """把（项目，角色，工程）解析成一份完整的派工配置。project 可为 None（只用全局角色、自己给目录）。"""
    role = dict(_merged_role(project, find_role_id(project, role_ref)))
    role["default_channels"] = role.pop("channels", "none")  # 角色里的 channels 是「默认占哪些通道」
    if not project:
        role.update(name=f":{role['role']}", channels=[])
        return role
    allowed = role["repos"] or list(project["repos"])
    unknown = [r for r in role["repos"] if r not in project["repos"]]
    if unknown:
        raise DispatchError(f"角色 {role['role']} 指向的工程 {'、'.join(unknown)} 不在项目 {project['id']} 的工程清单里")
    if repo_id:
        if repo_id not in project["repos"]:
            raise DispatchError(f"项目 {project['id']} 没有工程 {repo_id!r}，可选：{'、'.join(project['repos'])}")
        if repo_id not in allowed:
            raise DispatchError(f"角色 {role['title']} 不负责工程 {repo_id}（它负责：{'、'.join(allowed)}）")
    elif len(allowed) == 1:
        repo_id = allowed[0]
    elif detected_repo in allowed:
        repo_id = detected_repo
    else:
        raise DispatchError(f"角色 {role['title']} 负责多个工程，请用 --repo 指明：{'、'.join(allowed)}")
    return with_repo(role, project, repo_id, f"{project['id']}:{role['role']}@{repo_id}")


def with_repo(config, project, repo_id, name):
    repo = project["repos"][repo_id]
    return {**config, "name": name, "project_id": project["id"], "repo": repo_id, "cwd": repo["cwd"],
            "group": project["name"], "project": repo["name"], "channels": repo["channels"], "channel_ns": repo["channel_ns"]}


def bare(project, repo_id):
    """项目和工程已知但没指定角色：只带上目录和归属，不带模板和闸门。"""
    return with_repo({"default_channels": "none", **(project.get("defaults") or {})}, project, repo_id,
                     f"{project['id']}:@{repo_id}")


def load(name):
    """按台账里记的名字（「项目ID:角色ID@工程ID」）重新解析配置；定义被删掉时退回空配置。"""
    project_id, _, rest = name.partition(":")
    role_id, _, repo_id = rest.partition("@")
    try:
        project = load_project(project_id) if project_id else None
        if role_id:
            return resolve(project, role_id, repo_id or None)
        return bare(project, repo_id) if project and repo_id else {"name": name}
    except DispatchError:
        return {"name": name}


ROLE_BRIEF_TEMPLATE = """# 派工单 · {{job}}

你是一名**%s**，是本作业的执行者。你在项目 {{project}} 的工程 {{repo}}（`{{cwd}}`）里工作。
派工的主管不在场，执行过程中没有人能回答你的提问。

## 任务

{{task}}

## 约定

- 先读本工程及其上级目录的 `AGENTS.md`（如有）并遵守。
- （在这里写这个角色的职责、边界、禁止事项。）
- 做不了或拿不准的事不要硬做，以 `BLOCKED` 或 `NEEDS_DECISION` 结束，并把问题写进 `QUESTION`。

## 报告格式（最后一条回复必须以此结尾，逐行、不要加粗、不要放进代码块）

STATUS: PASS | FAIL | BLOCKED | NEEDS_DECISION
SUMMARY: 一句话结论
EVIDENCE: 关键产物的路径，逗号分隔
QUESTION: 仅 BLOCKED / NEEDS_DECISION 时填写
"""


def add_role(role_id, project=None, title=None, description=None, repos=None, backend=None, mode=None):
    store.check_name(role_id)
    directory = _project_role_dirs(project)[0] if project else LOCAL_ROLE_DIR
    path = os.path.join(directory, f"{role_id}.json")
    if os.path.exists(path):
        raise DispatchError(f"角色已存在：{path}")
    data = {}
    if title:
        data["title"] = title
    if description:
        data["description"] = description
    if repos:
        missing = [r for r in repos if not project or r not in project["repos"]]
        if missing:
            raise DispatchError(f"工程 {'、'.join(missing)} 不在项目的工程清单里")
        data["repos"] = repos
    if backend:
        backends.get(backend)
        data["backend"] = backend
    if mode:
        data["mode"] = mode
    base = _global_role(role_id)[0] if project else _find_role([SHIPPED_ROLE_DIR], role_id)[0]
    inherits = base is not None
    if not inherits:  # 全新角色给一套完整的起点；覆盖全局角色时只写要改的项
        data = {"title": title or role_id, "description": description or "（一句话说明这个角色做什么）",
                "backend": "opencode", "mode": "headless", "channels": "none", "timeout_min": 60,
                "statuses": ["PASS", "FAIL", "BLOCKED", "NEEDS_DECISION"],
                "gates": [{"type": "exit_ok"}, {"type": "status_reported"}], **data}
        store.write_text(os.path.join(directory, f"{role_id}.brief.md"), ROLE_BRIEF_TEMPLATE % (title or role_id))
    _write_json(path, data)
    return path, inherits


def set_pool(project, role_ref, pool):
    """写模型池。project 为 None 时写全局角色（~/.agent-dispatch/roles，不改随工具发布的文件）；
    否则写项目的角色定义。没有对应文件时建一个只含覆盖项的。pool 为空表示去掉这一层的模型池。"""
    from . import models
    role_id = find_role_id(project, role_ref)
    if pool:
        models.check_pool(pool)
    directory = _project_role_dirs(project)[0] if project else LOCAL_ROLE_DIR
    path = os.path.join(directory, f"{role_id}.json")
    data = _read_json(path) or {}
    if pool:
        data["model_pool"] = [{"model": e["model"], "weight": e["weight"]} for e in pool]
        data.pop("model", None)  # 有模型池就不再用固定模型，免得两处说法不一
        data.pop("models", None)
    else:
        data.pop("model_pool", None)
    _write_json(path, data)
    return path


# ---------- 配置中心：角色字段的分层读写 ----------

# 网页上允许修改的角色字段；brief_template 等内部字段不开放。
# mcp / skills：存登记 ID（登记表见 registry.py，同名不同内容各成一条）；
# 字段不存在 = 不管理（派工时不注入，执行者照旧用自己的配置）；
# 字段存在（哪怕是空列表）= 由 agent-dispatch 管理，派工时只带这些（注入在后续版本实现）。
EDITABLE_ROLE_FIELDS = {"title", "description", "repos", "backend", "model", "models", "mode", "channels",
                        "timeout_min", "statuses", "gates", "reply_suffix", "runs_root", "backend_args",
                        "model_pool", "mcp", "skills"}


def _layers(project, role_id):
    """从下到上列出各层 [(来源标签, 目录, 该层 json 或 None)]：随工具发布 → 本机全局 → 项目默认 → 项目。"""
    store.check_name(role_id)  # 角色ID直接拼路径，先挡住穿越
    shipped = _read_json(os.path.join(SHIPPED_ROLE_DIR, f"{role_id}.json"))
    local = _read_json(os.path.join(LOCAL_ROLE_DIR, f"{role_id}.json"))
    project_dir = _project_role_dirs(project)[0] if project else None
    project_role = _read_json(os.path.join(project_dir, f"{role_id}.json")) if project_dir else None
    defaults = (project or {}).get("defaults") or None
    return [("随工具发布", SHIPPED_ROLE_DIR, shipped), ("本机全局", LOCAL_ROLE_DIR, local),
            ("项目默认", None, dict(defaults) if defaults else None), ("项目", project_dir, project_role)]


def _require_role(project, role_id):
    if not any(data for _, _, data in _layers(project, role_id)):
        where = f"项目 {project['id']} 和全局" if project else "全局"
        raise DispatchError(f"{where}都没有角色 {role_id!r}")


def _check_field_values(project, fields):
    if not isinstance(fields, dict):
        raise DispatchError("字段表必须是「字段名: 值」的对象")
    unknown = [k for k in fields if k not in EDITABLE_ROLE_FIELDS]
    if unknown:
        raise DispatchError(f"不允许修改字段：{'、'.join(unknown)}。可修改：{'、'.join(sorted(EDITABLE_ROLE_FIELDS))}")
    if "repos" in fields:
        repos = fields["repos"]
        if not isinstance(repos, list) or not repos:
            raise DispatchError("repos 必须是非空的工程 ID 列表")
        if not project:
            raise DispatchError("全局角色不能绑定工程（repos）")
        missing = [r for r in repos if r not in project["repos"]]
        if missing:
            raise DispatchError(f"工程 {'、'.join(missing)} 不在项目 {project['id']} 的工程清单里")
    if fields.get("backend") and fields["backend"] not in backends.load_all():
        raise DispatchError(f"后端 {fields['backend']!r} 不在 backends.json 里，可用：{'、'.join(sorted(backends.load_all()))}")
    if "mode" in fields and fields["mode"] not in ("headless", "tmux"):
        raise DispatchError(f"mode 只能是 headless 或 tmux，不是 {fields['mode']!r}")
    if fields.get("model_pool"):
        from . import models
        models.check_pool(fields["model_pool"])
    from . import registry  # 延迟导入避免环
    for key in ("mcp", "skills"):
        if key not in fields:
            continue
        value = fields[key]
        if not isinstance(value, list) or not all(isinstance(n, str) and n for n in value):
            raise DispatchError(f"{key} 必须是{' MCP 服务' if key == 'mcp' else '技能'}登记 ID 的列表；"
                                "空列表表示「由 agent-dispatch 管理，一个都不带」")
        known = registry.load_mcp() if key == "mcp" else registry.load_skills()
        visible = registry.entries_for(known, project["id"] if project else "")
        missing = [n for n in value if n not in known]
        if missing:
            raise DispatchError(f"{key} 里的 {'、'.join(missing)} 还没登记。先导入或登记：agent-dispatch {key} import / add")
        foreign = [n for n in value if n in known and n not in visible]
        if foreign:
            owners = "、".join(sorted({registry.entry_project(known[n]) for n in foreign}))
            where = f"项目 {project['id']}" if project else "全局角色"
            raise DispatchError(f"{key} 里的 {'、'.join(foreign)} 归属别的项目（{owners}），{where}不能使用；"
                                f"重新导入让它们归属本项目，或换本项目的 / 全局的条目")
        names = [visible[n].get("name") or n for n in value]  # 下发给执行者的服务名，同一个生效集合里不能重复
        for name in sorted(set(names)):
            ids = [n for n in value if (known[n].get("name") or n) == name]
            if len(ids) > 1:
                raise DispatchError(f"{key} 里选了两个同名的{' MCP 服务' if key == 'mcp' else '技能'}：{name}"
                                    f"（{' 与 '.join(ids)}）。执行者按名字调用，一个角色只能留一个")


def _layer_dir(project):
    return _project_role_dirs(project)[0] if project else LOCAL_ROLE_DIR


def _layer_json_path(project, role_id):
    store.check_name(role_id)
    return os.path.join(_layer_dir(project), f"{role_id}.json")


def role_detail(project, role_id):
    """角色的完整视图：合并后的值、每个字段的来源层、派工单模板及其来源和路径。"""
    layers = _layers(project, role_id)
    _require_role(project, role_id)
    merged, sources = {}, {}
    for label, _, data in layers:
        for key, value in (data or {}).items():
            if key == "brief_template":
                continue
            if key == "repo":  # 单数写法与 repos 等价
                key, value = "repos", [value]
            merged[key] = value
            sources[key] = label
    pool_source = "项目" if sources.get("model_pool") == "项目" else ("全局" if sources.get("model_pool") else None)
    return {
        "role": role_id,
        "title": merged.get("title") or role_id,
        "scope": "全局" if not any(d for l, _, d in layers if l == "项目") else
                 ("项目覆盖全局" if any(d for l, _, d in layers if l in ("随工具发布", "本机全局")) else "项目专属"),
        "repos": merged.get("repos") or [],
        "pool_source": pool_source,
        "fields": {key: {"value": merged[key], "source": sources[key]} for key in sorted(merged)},
        "brief": get_brief(project, role_id),
    }


def set_role_fields(project, role_id, fields):
    """把字段写到这一层（有项目写项目角色，否则写本机全局）；永远不改随工具发布的 roles/。
    各层都不存在的角色不能在这里凭空新建（新建走 add_role）。"""
    _require_role(project, role_id)
    _check_field_values(project, fields)
    path = _layer_json_path(project, role_id)
    data = _read_json(path) or {}
    data.update(fields)
    _write_json(path, data)
    return path


def reset_role_fields(project, role_id, keys):
    """从这一层删掉字段，恢复继承上一层的值。"""
    path = _layer_json_path(project, role_id)
    data = _read_json(path)
    if data is None:
        layer = f"项目 {project['id']}" if project else "本机全局"
        raise DispatchError(f"{layer}这一层没有角色 {role_id} 的定义文件，无从恢复继承")
    for key in keys:
        data.pop(key, None)
    _write_json(path, data)
    return path


def _layer_brief_dirs(project):
    """模板从上层往下找：项目角色 → 本机全局 → 随工具发布。"""
    dirs = [("项目", _project_role_dirs(project)[0])] if project else []
    return dirs + [("本机全局", LOCAL_ROLE_DIR), ("随工具发布", SHIPPED_ROLE_DIR)]


def _brief_file(directory, data, role_id):
    """某一层 json 决定的模板文件名（与 _find_role 同一规则）。"""
    return os.path.join(directory, data.get("brief_template") or f"{role_id}.brief.md")


def get_brief(project, role_id):
    """与派工（_merged_role 的 _brief_path）同一套规则：从上层往下，第一个有角色 json 且
    模板文件存在的层胜出；某一层有 json 但模板文件不在时落到下一层（与 _find_role 的回落一致）。"""
    _require_role(project, role_id)
    for label, directory in _layer_brief_dirs(project):
        data = _read_json(os.path.join(directory, f"{role_id}.json"))
        if data is None:
            continue
        path = _brief_file(directory, data, role_id)
        if os.path.isfile(path):
            return {"text": store.read_text(path, ""), "source": label, "path": path}
    return {"text": None, "source": None, "path": None}


def set_brief(project, role_id, text):
    """写这一层的模板。这一层还没有角色 json 时先建一个空定义（{}），否则按 _find_role 的规则
    这一层不参与模板选择，会出现界面显示已保存、派工却不生效。"""
    _require_role(project, role_id)
    path = _layer_json_path(project, role_id)
    data = _read_json(path)
    if data is None:
        data = {}
        _write_json(path, data)
    brief = _brief_file(_layer_dir(project), data, role_id)
    store.write_text(brief, text)
    return brief


def reset_brief(project, role_id):
    """删掉这一层的模板，回落到上一层的模板。"""
    path = _layer_json_path(project, role_id)
    data = _read_json(path)
    layer = f"项目 {project['id']}" if project else "本机全局"
    if data is None:
        raise DispatchError(f"{layer}这一层没有 {role_id} 的定义文件，无从读模板")
    brief = _brief_file(_layer_dir(project), data, role_id)
    if not os.path.isfile(brief):
        raise DispatchError(f"{layer}这一层没有模板文件 {os.path.basename(brief)}，不需要恢复继承")
    os.remove(brief)
    return brief


def delete_role_layer(project, role_id):
    """删除这一层的角色定义和模板（项目层或本机全局层）；随工具发布的角色不能删。"""
    path = _layer_json_path(project, role_id)
    data = _read_json(path)
    if data is None:
        layer = f"项目 {project['id']}" if project else "本机全局"
        raise DispatchError(f"{layer}这一层没有角色 {role_id} 的定义；随工具发布的角色不能删除，只能在本机全局或项目层覆盖")
    os.remove(path)
    brief = _brief_file(_layer_dir(project), data, role_id)
    if os.path.isfile(brief):
        os.remove(brief)
    return path
