"""角色并发上限：同一角色在同一环境里最多同时几张单，以及项目里一组角色合计最多几张。

- 角色的 max_parallel：按（项目，角色，环境）计数。测试类角色写 1，同一环境同一条线就只跑一张；
  不同环境互不占用。研发类角色不写，不限制。
- 项目的 limit_groups：{分组名: 上限}，角色用 limit_group 归到某个分组，分组内合计不超过上限
  （例如所有测试角色合计最多 4 张，用来封住同时开着的浏览器数量）。

满了不报错，新单进入排队（与模型并发满时同一套排队），有空位后按排队先后自动开始。
「占着位置」的作业：运行中、人工持有，或 tmux 界面还开着的（界面连同它的浏览器还活着）。
调用方要在 models._locked() 这把全局锁里做「检查 + 写台账」，并发派工才不会超发。
"""
from . import definitions, store, tmux
from .store import DispatchError


def _occupying(job, live_sessions):
    return store.effective_state(job) in ("running", "held") or (job.get("tmux") in live_sessions)


def blocked(job, exclude=None):
    """这张单现在能不能开始：能返回 None，不能返回一句「在等什么」。exclude 是不计入的作业名（它自己）。"""
    limit, group = job.get("max_parallel"), job.get("limit_group")
    if not job.get("project_id") or not (limit or group):
        return None
    group_limit = None
    if group:
        try:
            group_limit = definitions.load_project(job["project_id"])["limit_groups"].get(group)
        except DispatchError:
            group_limit = None
    if not limit and not group_limit:
        return None
    live = tmux.sessions()
    same_role, same_group = [], []
    for other in store.all_jobs():
        if other["name"] in (job["name"], exclude) or other.get("project_id") != job["project_id"]:
            continue
        if not _occupying(other, live):
            continue
        if other.get("role") == job.get("role") and (other.get("env") or None) == (job.get("env") or None):
            same_role.append(other["name"])
        if group and other.get("limit_group") == group:
            same_group.append(other["name"])
    title = job.get("role_title") or job.get("role") or "-"
    if limit and len(same_role) >= limit:
        where = f"在环境 {job['env']} " if job.get("env") else ""
        return f"角色「{title}」{where}的空位（上限 {limit}，现在是 {'、'.join(sorted(same_role))}）"
    if group_limit and len(same_group) >= group_limit:
        return f"分组 {group} 的空位（合计上限 {group_limit}，现在是 {'、'.join(sorted(same_group))}）"
    return None
