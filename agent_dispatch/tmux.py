"""tmux 运行方式的底层操作：执行者的交互界面跑在一个 tmux 会话里，你可以随时接进去。"""
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time

HUB_ENV = "AGENT_DISPATCH_HUB"  # 汇总会话的名字；设为空串表示不用汇总会话


def available():
    return shutil.which("tmux") is not None


def _tmux(*args, **kwargs):
    return subprocess.run(["tmux", *args], capture_output=True, text=True, **kwargs)


def alive(session):
    return bool(session) and available() and _tmux("has-session", "-t", f"={session}").returncode == 0


def new_session(session, cwd, argv, env=None):
    command = " ".join(f"{key}={shlex.quote(value)}" for key, value in (env or {}).items())
    command = f"env {command} {shlex.join(argv)}" if command else shlex.join(argv)
    result = _tmux("new-session", "-d", "-s", session, "-x", "200", "-y", "50", "-c", cwd, command)
    if result.returncode != 0:
        raise RuntimeError(f"tmux 会话启动失败：{result.stderr.strip()}")


def paste(session, text):
    """把文本作为一次粘贴送进界面再回车；多行文本不会被逐行提交。"""
    buffer = f"ad-{os.getpid()}"
    result = _tmux("load-buffer", "-b", buffer, "-", input=text)
    if result.returncode == 0:
        result = _tmux("paste-buffer", "-p", "-d", "-b", buffer, "-t", f"={session}:")
    if result.returncode != 0:
        raise RuntimeError(f"向 tmux 会话粘贴失败：{result.stderr.strip()}")


def press_enter(session):
    _tmux("send-keys", "-t", f"={session}:", "Enter")


def _pane_pids(session):
    result = _tmux("list-panes", "-s", "-t", f"={session}", "-F", "#{pane_pid}")
    return [int(p) for p in result.stdout.split() if p.isdigit()] if result.returncode == 0 else []


def _descendants(roots):
    """这些进程的全部后代（含自身）。执行者拉起的 MCP 服务、浏览器都在里面。"""
    result = subprocess.run(["ps", "-axo", "pid=,ppid="], capture_output=True, text=True)
    children = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            children.setdefault(int(parts[1]), []).append(int(parts[0]))
    seen, stack = set(), list(roots)
    while stack:
        pid = stack.pop()
        if pid not in seen:
            seen.add(pid)
            stack += children.get(pid, [])
    return seen


def _signal_all(pids, sig):
    alive_pids = []
    for pid in pids:
        try:
            os.kill(pid, sig)
            alive_pids.append(pid)
        except (ProcessLookupError, PermissionError):
            pass
    return alive_pids


def kill(session):
    if not alive(session):
        return
    try:  # 关界面前先记下它名下的全部进程：界面一关，没跟着退出的（残留的 MCP 服务、浏览器）要补杀
        tree = _descendants(_pane_pids(session))
    except (OSError, ValueError):
        tree = set()
    # 窗口挂进汇总会话后，只关会话不会结束里面的进程；要按窗口关，它会从所有会话里一起消失
    for window in _window_ids(session):
        _tmux("kill-window", "-t", window)
    if alive(session):
        _tmux("kill-session", "-t", f"={session}")
    if tree:
        for _ in range(10):  # 给执行者 2 秒自己收尾（它会依次关掉自己的 MCP 服务）
            if not _signal_all(tree, 0):
                return
            time.sleep(0.2)
        if _signal_all(tree, signal.SIGTERM):
            time.sleep(0.5)
            _signal_all(tree, signal.SIGKILL)


def attach_command(session):
    return f"tmux attach -t {shlex.quote(session)}"


# ---------- 汇总会话：所有作业界面挂在同一个 tmux 会话里，接一次就能看到全部 ----------

def hub_name():
    return os.environ.get(HUB_ENV, "agent-dispatch")


def hub_attach_command():
    return f"tmux attach -t {shlex.quote(hub_name())}"


def sessions():
    result = _tmux("list-sessions", "-F", "#{session_name}") if available() else None
    return set(result.stdout.split("\n")) - {""} if result and result.returncode == 0 else set()


def _window_ids(session):
    result = _tmux("list-windows", "-t", f"={session}", "-F", "#{window_id}")
    return result.stdout.split() if result.returncode == 0 else []


def ensure_hub():
    """汇总会话不在就建一个，首个窗口是作业总览；返回会话名，没启用或建不成时返回 None。"""
    hub = hub_name()
    if not hub or not available():
        return None
    if not alive(hub):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = {"PYTHONPATH": root, **{key: os.environ[key] for key in ("AGENT_DISPATCH_HOME", "AGENT_DISPATCH_BACKENDS")
                                      if os.environ.get(key)}}
        command = " ".join(f"{key}={shlex.quote(value)}" for key, value in env.items())
        command = f"env {command} {shlex.join([sys.executable, '-m', 'agent_dispatch', 'status', '--watch'])}"
        _tmux("new-session", "-d", "-s", hub, "-n", "总览", "-x", "200", "-y", "50", command)
        if not alive(hub):  # 两个作业同时建时，后到的那个会失败，但会话已经在了
            return None
    return hub


def link_to_hub(session, title=None):
    """把作业界面的窗口挂进汇总会话并以作业名命名；已经挂过的不重复挂。挂不上只返回 False，不影响作业。"""
    hub = ensure_hub()
    if not hub or not alive(session):
        return False
    linked = set(_window_ids(hub))
    for window in _window_ids(session):
        if title:
            _tmux("rename-window", "-t", window, title)
        if window not in linked:
            _tmux("link-window", "-d", "-s", window, "-t", f"={hub}:")
    return True
