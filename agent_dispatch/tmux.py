"""tmux 运行方式的底层操作：执行者的交互界面跑在一个 tmux 会话里，你可以随时接进去。"""
import os
import shlex
import shutil
import subprocess


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


def kill(session):
    if alive(session):
        _tmux("kill-session", "-t", f"={session}")


def attach_command(session):
    return f"tmux attach -t {shlex.quote(session)}"
