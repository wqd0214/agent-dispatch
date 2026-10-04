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
GLOBAL_ROLE_DIRS = [os.path.join(store.HOME, "roles"), os.path.join(backends.ROOT, "roles")]


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


def add_repo(project_id, repo_id, cwd, channels=None):
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
    data.setdefault("repos", {})[repo_id] = {**(data.get("repos") or {}).get(repo_id, {}), **repo}
    _write_json(_project_file(project_id), data)


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


def _merged_role(project, role_id):
    global_role, global_brief = _find_role(GLOBAL_ROLE_DIRS, role_id)
    project_role, project_brief = _find_role(_project_role_dirs(project), role_id)
    if global_role is None and project_role is None:
        return None
    merged = {**(global_role or {}), **((project or {}).get("defaults") or {}), **(project_role or {})}
    merged.pop("brief_template", None)
    repos = merged.pop("repos", None) or ([merged["repo"]] if merged.get("repo") else [])
    merged.pop("repo", None)
    merged.update(role=role_id, title=merged.get("title") or role_id, repos=repos,
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
    directory = _project_role_dirs(project)[0] if project else GLOBAL_ROLE_DIRS[0]
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
    inherits = project is not None and _find_role(GLOBAL_ROLE_DIRS, role_id)[0] is not None
    if not inherits:  # 全新角色给一套完整的起点；覆盖全局角色时只写要改的项
        data = {"title": title or role_id, "description": description or "（一句话说明这个角色做什么）",
                "backend": "opencode", "mode": "headless", "channels": "none", "timeout_min": 60,
                "statuses": ["PASS", "FAIL", "BLOCKED", "NEEDS_DECISION"],
                "gates": [{"type": "exit_ok"}, {"type": "status_reported"}], **data}
        store.write_text(os.path.join(directory, f"{role_id}.brief.md"), ROLE_BRIEF_TEMPLATE % (title or role_id))
    _write_json(path, data)
    return path, inherits
