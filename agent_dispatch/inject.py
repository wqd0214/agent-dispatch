"""按作业下发角色配置的 MCP 服务与技能（不改工程目录、不改用户的 pi / OpenCode 配置）。

角色解析后：mcp / skills 字段不存在 = 不管理，派工命令与原先完全一样；
字段存在（哪怕是空列表）= 由 agent-dispatch 管理，只带选中的条目。两个字段各自独立判断。

下发文件统一放该作业自己的目录下（~/.agent-dispatch/jobs/<作业名>/inject/），
每轮派工前重新生成（幂等），作业结束（验收 / 驳回 / 叫停）后删除。

- pi：技能用 --no-skills --skill <目录>（只留选中的）；MCP 用 PI_CODING_AGENT_DIR 指向作业
  私有的临时 agent 目录——mcp.json 从登记表生成，auth.json / models.json / sessions 等用符号
  链接指回 ~/.pi/agent 下的原文件（绝不复制或改写），再加 --no-approve 不读工程 .pi/mcp.json。
  实测（docs/design/mcp-injection-research.md 与本作业复测）：这样用户全局与工程级的服务都
  不会加载；副作用是不加载 ~/.pi/agent/settings.json 里配置的扩展包（superpowers 等）。
- OpenCode：OPENCODE_CONFIG_CONTENT 注入——mcp 段写选中服务的完整定义；工程 opencode.json
  和全局配置里有、但本次没选中的服务写「同名完整定义 + enabled:false」关掉（只读这些文件）；
  技能写 skills.paths。headless 命令要加 --standalone，env 配置才生效。全局技能无法排除，
  只能追加。凭据都在注入的配置里，文件权限 600，命令行与输出不出现明文。
"""
import json
import os
import shlex
import shutil

from . import store
from .store import DispatchError

INJECT_DIR = "inject"
# 换 agent 目录时符号链接回 ~/.pi/agent 的文件：凭据、模型与会话。
# 不链接 settings.json / git / npm / extensions：settings 里的扩展包会把用户全局技能带进来
PI_AGENT_LINKS = ("auth.json", "models.json", "models-store.json", "sessions")
# 角色只管 MCP、不管技能时再多链接这些：用户的设置（重试策略等）、扩展包与技能照常生效，
# 项目信任的记录也带上（工程自己的 .pi 扩展、技能照常加载），只有用户全局的 MCP 服务换成选中的那几个
PI_AGENT_LINKS_UNMANAGED_SKILLS = ("settings.json", "trust.json", "extensions", "git", "npm", "skills", "bin", "install")
# 登记条目里可以写的占位符：派工时按这张单的实际情况填（如每个角色每个环境一个独立的浏览器目录）
PLACEHOLDERS = ("env", "job", "role", "project", "repo", "cwd")


def pi_agent_source():
    return os.environ.get("AGENT_DISPATCH_PI_AGENT_DIR") or os.path.join(os.path.expanduser("~"), ".pi", "agent")


def opencode_global_config():
    return os.environ.get("AGENT_DISPATCH_OPENCODE_GLOBAL") or \
        os.path.join(os.path.expanduser("~"), ".config", "opencode", "opencode.json")


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


# ---------- 角色字段 → 选中的登记条目 ----------

def selection(profile, env=None):
    """角色解析结果 → {mcp: [{id,name,entry}], skills: [{id,name,path}]}；字段不存在 = 不管理。
    mcp 的登记 ID 里的 {{env}} 换成这次派工选的环境。"""
    from . import definitions, registry
    sel = {}
    if "mcp" in (profile or {}):
        reg = registry.load_mcp()
        ids = list(profile["mcp"] or [])
        if not env:  # 没带环境：按环境选的服务这次不下发（角色要求必须带环境的，派工时已先拦下）
            ids = [i for i in ids if definitions.ENV_TOKEN not in i]
        profile = {**profile, "mcp": [definitions.expand_env(i, env) for i in ids]}
        missing = [i for i in profile["mcp"] or [] if i not in reg]
        if missing:
            raise DispatchError(f"角色选的 MCP 服务还没登记：{'、'.join(missing)}。先导入或登记：agent-dispatch mcp import / add")
        sel["mcp"] = [{"id": i, "name": reg[i].get("name") or i, "entry": reg[i]} for i in profile["mcp"] or []]
    if "skills" in (profile or {}):
        reg = registry.load_skills()
        missing = [i for i in profile["skills"] or [] if i not in reg]
        if missing:
            raise DispatchError(f"角色选的技能还没登记：{'、'.join(missing)}。先导入或登记：agent-dispatch skills import / add")
        sel["skills"] = [{"id": i, "name": reg[i].get("name") or i, "path": reg[i]["path"]} for i in profile["skills"] or []]
    return sel


def summarize(sel):
    """存进台账的部分：只有登记 ID 和名字，不含凭据。"""
    if not sel:
        return None
    return {"mcp": [{"id": s["id"], "name": s["name"]} for s in sel["mcp"]] if "mcp" in sel else None,
            "skills": [{"id": s["id"], "name": s["name"], "path": s["path"]} for s in sel["skills"]] if "skills" in sel else None}


def _selection_of_job(job):
    """按台账里记的摘要重新解析出完整条目（每轮派工前调用，登记表更新后会用到新内容）。"""
    summary = job.get("inject")
    if not summary or (summary.get("mcp") is None and summary.get("skills") is None):
        return {}
    from . import registry
    mreg, sreg = registry.load_mcp(), registry.load_skills()
    sel = {}
    if summary.get("mcp") is not None:
        missing = [s["id"] for s in summary["mcp"] if s["id"] not in mreg]
        if missing:
            raise DispatchError(f"作业选的 MCP 服务已从登记表删除：{'、'.join(missing)}")
        sel["mcp"] = [{"id": s["id"], "name": s["name"], "entry": mreg[s["id"]]} for s in summary["mcp"]]
    if summary.get("skills") is not None:
        missing = [s["id"] for s in summary["skills"] if s["id"] not in sreg]
        if missing:
            raise DispatchError(f"作业选的技能已从登记表删除：{'、'.join(missing)}")
        sel["skills"] = [{"id": s["id"], "name": s["name"], "path": sreg[s["id"]]["path"]} for s in summary["skills"]]
    return sel


# ---------- 生成下发文件 ----------

def _fill(value, values):
    """把 {{env}} / {{job}} 等占位符填进登记条目的字符串里（列表、对象逐层处理）。"""
    if isinstance(value, str):
        for key, text in values.items():
            value = value.replace("{{" + key + "}}", text)
        return value
    if isinstance(value, list):
        return [_fill(item, values) for item in value]
    if isinstance(value, dict):
        return {key: _fill(item, values) for key, item in value.items()}
    return value


def _fill_entry(entry, name, job):
    values = {"env": job.get("env") or "", "job": name, "role": job.get("role") or "",
              "project": job.get("project_id") or "", "repo": job.get("repo") or "", "cwd": job.get("cwd") or ""}
    return {**entry, **{key: _fill(entry[key], values) for key in ("command", "args", "env", "url", "headers") if key in entry}}


def _pi_server(entry):
    """登记条目 → pi 的 mcp.json 写法。"""
    extra = {"exposure": entry["exposure"]} if entry.get("exposure") else {}
    if entry.get("type") == "remote" or entry.get("url"):
        server = {"url": entry["url"], **extra}
        if entry.get("headers"):
            server["headers"] = dict(entry["headers"])
        return server
    server = {"command": entry.get("command") or "", **extra}
    if entry.get("args"):
        server["args"] = list(entry["args"])
    if entry.get("env"):
        server["env"] = dict(entry["env"])
    return server


def _oc_server(entry, enabled):
    """登记条目 → OpenCode 配置写法（local 的 command 是数组）。"""
    if entry.get("type") == "remote" or entry.get("url"):
        server = {"type": "remote", "url": entry["url"], "enabled": enabled}
        if entry.get("headers"):
            server["headers"] = dict(entry["headers"])
        return server
    server = {"type": "local", "command": [entry.get("command") or "", *(entry.get("args") or [])], "enabled": enabled}
    if entry.get("env"):
        server["env"] = dict(entry["env"])
    return server


def _oc_mcp_section(path):
    data = _read_json(path)
    servers = (data or {}).get("mcp") if isinstance(data, dict) else None
    return servers.items() if isinstance(servers, dict) else []


def prepare(name, backend, job):
    """（重新）生成这个作业的下发文件，返回 (命令行参数, 环境变量)。每轮派工前调用，幂等。"""
    sel = _selection_of_job(job)
    if not sel:
        return [], {}
    if "mcp" in sel:  # 占位符按这张单的环境、角色、目录填好
        sel["mcp"] = [{**s, "entry": _fill_entry(s["entry"], name, job)} for s in sel["mcp"]]
    directory = store.job_path(name, INJECT_DIR)
    shutil.rmtree(directory, ignore_errors=True)
    os.makedirs(directory)
    args, env = [], {}
    fmt = backend.get("format")
    if fmt == "pi":
        if "skills" in sel:
            args.append("--no-skills")
            for skill in sel["skills"]:
                args += ["--skill", skill["path"]]
        if "mcp" in sel:
            agent = os.path.join(directory, "pi-agent")
            os.makedirs(agent)
            source = pi_agent_source()
            links = PI_AGENT_LINKS + (() if "skills" in sel else PI_AGENT_LINKS_UNMANAGED_SKILLS)
            for item in links:
                target = os.path.join(source, item)
                if os.path.exists(target):
                    os.symlink(target, os.path.join(agent, item))
            servers = {s["name"]: _pi_server(s["entry"]) for s in sel["mcp"]}
            path = os.path.join(agent, "mcp.json")
            store.write_text(path, json.dumps({"mcpServers": servers}, ensure_ascii=False, indent=2) + "\n")
            os.chmod(path, 0o600)
            env["PI_CODING_AGENT_DIR"] = agent
            if "skills" in sel:
                # 严格模式：不读工程 .pi 下的任何资源（project trust），保证只带选中的服务与技能
                args.append("--no-approve")
            # 只管 MCP 时不加：工程自己的扩展、技能和 .pi/mcp.json 仍按用户已有的信任记录加载，
            # 换掉的只是用户全局那一层
    elif fmt == "opencode":
        config = {}
        if "mcp" in sel:
            mcp = {s["name"]: _oc_server(s["entry"], True) for s in sel["mcp"]}
            # 工程与全局配置里有、但本次没选中的：同名完整定义 + enabled:false 关掉
            for path in (os.path.join(job["cwd"], "opencode.json"), opencode_global_config()):
                for svc_name, raw in _oc_mcp_section(path):
                    if svc_name in mcp or not isinstance(raw, dict) or raw.get("enabled") is False:
                        continue
                    if raw.get("command") is None and raw.get("url") is None:
                        continue  # 只有 enabled 的覆盖条目本来就不生效
                    disabled = json.loads(json.dumps(raw))  # 深拷贝，不改动来源
                    disabled["enabled"] = False
                    mcp[svc_name] = disabled
            config["mcp"] = mcp
        if "skills" in sel:
            config["skills"] = {"paths": [s["path"] for s in sel["skills"]]}
        path = os.path.join(directory, "opencode-config.json")
        store.write_text(path, json.dumps(config, ensure_ascii=False) + "\n")
        os.chmod(path, 0o600)
        env["OPENCODE_CONFIG_CONTENT"] = store.read_text(path)
        # env 配置只在独立进程里生效（调研实验 5：不带 --standalone 时后台服务读不到它）
        args.append("--standalone")
    else:
        raise DispatchError(f"后端 {job.get('backend')} 的 format={fmt!r} 不支持按作业下发 MCP / 技能")
    return args, env


def runtime(name, backend, job):
    """运行器每轮取下发参数；没管理任何字段时不生成文件、命令与原先完全一样。"""
    return prepare(name, backend, job)


def merge_tmux_opencode(argv, env):
    """tmux 的 opencode 模板自带 env OPENCODE_CONFIG_CONTENT={"model":…}，把下发配置并进那个值。

    内层 env 赋值会盖掉 tmux 层的同一变量，所以必须合并而不是各设各的。
    """
    if "OPENCODE_CONFIG_CONTENT" not in env:
        return env
    for i, token in enumerate(argv):
        if token.startswith("OPENCODE_CONFIG_CONTENT="):
            base = _read_json_value(token.split("=", 1)[1]) or {}
            base.update(json.loads(env.pop("OPENCODE_CONFIG_CONTENT")))
            argv[i] = "OPENCODE_CONFIG_CONTENT=" + json.dumps(base, ensure_ascii=False)
            break
    return env


def _read_json_value(text):
    try:
        return json.loads(text)
    except ValueError:
        return None


def cleanup(name):
    """作业结束后删掉 inject/ 里含凭据的文件。"""
    shutil.rmtree(store.job_path(name, INJECT_DIR), ignore_errors=True)


def takeover_prefix(name, backend, job):
    """status 里「接手」命令要带的前后缀：环境变量前缀与命令行参数（凭据只引用文件）。"""
    try:
        sel = _selection_of_job(job)
    except DispatchError:  # 登记条目被删等情况：接手命令退回原样
        return [], []
    if not sel:
        return [], []
    fmt = backend.get("format")
    if fmt != "pi" and fmt != "opencode":
        return [], []
    directory = store.job_path(name, INJECT_DIR)
    if not os.path.isdir(directory):  # 作业已结束、文件已清理：接手时重新生成
        prepare(name, backend, job)
    prefix, suffix = [], []
    if fmt == "pi":
        if "skills" in sel:
            suffix = ["--no-skills"] + [part for s in sel["skills"] for part in ("--skill", s["path"])]
        if "mcp" in sel:
            prefix = [f"PI_CODING_AGENT_DIR={os.path.join(directory, 'pi-agent')}"]
            if "skills" in sel:
                suffix = suffix + ["--no-approve"]
    else:
        config = os.path.join(directory, "opencode-config.json")
        if os.path.isfile(config):
            prefix = [f'OPENCODE_CONFIG_CONTENT="$(cat {shlex.quote(config)})"']
            suffix = ["--standalone"]
    return prefix, suffix


# ---------- 给预览 / status / 接口的说明 ----------

def describe(sel, fmt):
    """「本次下发」的说明（不含凭据）：管理了哪些 MCP（name 与登记 ID）、哪些技能、各自的局限。"""
    if not sel:
        return {"managed": False, "note": "未管理：执行者使用自己的配置"}
    out = {"managed": True}
    if "mcp" in sel:
        note = ("严格模式：换用作业私有的临时 agent 目录（符号链接回 ~/.pi/agent 的凭据与会话），"
                "用户全局的服务不会加载，只带选中的；"
                + ("~/.pi/agent/settings.json 里的扩展包（及其技能）也不加载" if "skills" in sel else
                   "技能未管理：用户的设置、扩展包与技能照常加载，工程自己的 .pi 资源（含 .pi/mcp.json）按已有的信任记录照常加载")
                if fmt == "pi" else
                "选中的服务经 OPENCODE_CONFIG_CONTENT 注入；工程 opencode.json 与全局配置里其余服务"
                "以同名完整定义 + enabled:false 关闭；headless 命令加 --standalone")
        out["mcp"] = {"services": [{"id": s["id"], "name": s["name"]} for s in sel["mcp"]], "note": note}
    if "skills" in sel:
        note = ("--no-skills 后只加载选中的技能目录" if fmt == "pi" else
                "技能目录经 skills.paths 注入；无法排除执行者自己的全局技能，只能追加")
        out["skills"] = {"skills": [{"id": s["id"], "name": s["name"]} for s in sel["skills"]], "note": note}
    return out


def describe_job(job, backend=None):
    if backend is None:
        from . import backends
        backend = backends.get(job["backend"])
    try:
        sel = _selection_of_job(job)
    except DispatchError as exc:  # 登记条目被删等情况：详情页不能因此打不开
        return {"managed": True, "note": f"登记条目已不可用：{exc}"}
    return describe(sel, backend.get("format"))


def render(desc):
    """describe 的结果 → status / 派工单预览里的几行文字。"""
    if not desc.get("managed"):
        return ["未管理：执行者使用自己的配置"]
    lines = []
    if "mcp" in desc:
        services = "、".join(f"{s['name']}（{s['id']}）" for s in desc["mcp"]["services"]) or "（一个都不带）"
        lines.append(f"MCP：{services}。{desc['mcp']['note']}")
    if "skills" in desc:
        skills = "、".join(f"{s['name']}（{s['id']}）" for s in desc["skills"]["skills"]) or "（一个都不带）"
        lines.append(f"技能：{skills}。{desc['skills']['note']}")
    return lines or ["未管理：执行者使用自己的配置"]


def role_notes():
    """roles show 里对 mcp / skills 字段的下发说明。"""
    return ["下发说明：mcp / skills 字段存在（哪怕是空列表）= 派工时由 agent-dispatch 管理并只带选中的条目；",
            "  pi：技能 --no-skills --skill；MCP 换用作业私有的临时 agent 目录（PI_CODING_AGENT_DIR），"
            "用户全局与工程级服务不加载（settings.json 里的扩展包也不加载）。",
            "  OpenCode：OPENCODE_CONFIG_CONTENT 注入选中的服务（其余同名完整定义 + enabled:false 关闭）"
            "与 skills.paths；无法排除执行者自己的全局技能，只能追加。"]
