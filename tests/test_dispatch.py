"""端到端测试：用假智能体走完整的命令行流程，不依赖真实的 pi / opencode。"""
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["AGENT_DISPATCH_HUB"] = ""  # 默认不启用汇总会话，免得用例把窗口挂进本机真实的那个；cli() 里再换成用例自己的
FAKE = os.path.join(ROOT, "tests", "fake_agent.py")
FAKE_TUI = os.path.join(ROOT, "tests", "fake_tui.py")


class DispatchBase(unittest.TestCase):
    """公共的环境搭建与辅助方法，不含测试用例：各测试类继承它，
    避免基类的用例被每个子类重复跑一遍。"""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="ad-test-"))
        self.home = os.path.join(self.tmp, "home")
        self.work = os.path.join(self.tmp, "work")
        self.hub = "adhub-" + os.path.basename(self.tmp)
        os.makedirs(os.path.join(self.home, "profiles"))
        os.makedirs(os.path.join(self.work, "runs"))
        backends = {
            "fakepi": {"format": "pi", "session": "named", "default_model": "m",
                       "argv": [sys.executable, FAKE, "pi", "{session}", "{task}", "{model}"],
                       "resume_argv": [sys.executable, FAKE, "pi", "{session}", "{task}", "{model}"],
                       "attach": "fakepi --session-id {session}",
                       "interactive_argv": [sys.executable, FAKE_TUI, os.path.join(self.tmp, "sessions"), "{session}", "{task}"],
                       "session_reader": {"glob": os.path.join(self.tmp, "sessions", "*", "*_{session}.jsonl")}},
            "fakeoc": {"format": "opencode", "session": "assigned", "default_model": "m",
                       "argv": [sys.executable, FAKE, "opencode", "", "{task}"],
                       "resume_argv": [sys.executable, FAKE, "opencode", "{session}", "{task}"]},
            "missing": {"format": "pi", "session": "named", "default_model": "m",
                        "argv": ["no-such-agent-binary", "{task}"], "resume_argv": ["no-such-agent-binary", "{task}"]},
        }
        self.backends = os.path.join(self.tmp, "backends.json")
        with open(self.backends, "w") as f:
            json.dump(backends, f)
        profile = {
            "cwd": self.work, "backend": "fakepi", "channels": ["a", "b"], "default_channels": "all",
            "statuses": ["PASS", "FAIL", "BLOCKED", "NEEDS_DECISION"], "brief_template": "t.brief.md",
            "runs_root": "runs",
            "gates": [{"type": "exit_ok"}, {"type": "status_reported"}, {"type": "run_dir"},
                      {"type": "file", "path": "{run_dir}/report.md"}, {"type": "verdicts"}],
        }
        with open(os.path.join(self.home, "profiles", "t.json"), "w") as f:
            json.dump(profile, f)
        with open(os.path.join(self.home, "profiles", "t.brief.md"), "w") as f:
            f.write("派工单 {{job}} 通道 {{channels}}\n{{task}}\n")

    def tearDown(self):
        for name in self.jobs():
            self.cli("stop", name)
            subprocess.run(["tmux", "kill-session", "-t", "=ad-" + name], capture_output=True)
        subprocess.run(["tmux", "kill-session", "-t", "=" + self.hub], capture_output=True)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cli(self, *args, stdin=None, cwd=None, env_extra=None):
        env = dict(os.environ, AGENT_DISPATCH_HOME=self.home, AGENT_DISPATCH_BACKENDS=self.backends, PYTHONPATH=ROOT,
                   AGENT_DISPATCH_HUB=self.hub)  # 汇总会话用测试自己的名字，不碰本机真实的那个
        # 跑测试的进程可能带着派工者相关的环境变量，识别不能被它们污染
        for key in ("AGENT_DISPATCH_WORKER", "AGENT_DISPATCH_SESSION", "AGENT_DISPATCH_SESSION_KIND",
                    "CLAUDE_CODE_SESSION_ID", "TMUX_PANE", "TERM_SESSION_ID", "TMUX"):
            env.pop(key, None)
        env.update(env_extra or {})
        return subprocess.run([sys.executable, "-m", "agent_dispatch", *args], env=env, cwd=cwd,
                              capture_output=True, text=True, input=stdin, timeout=60)

    def ok(self, *args, **kw):
        result = self.cli(*args, **kw)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        return result

    def job(self, name):
        return json.loads(self.ok("status", name, "--json").stdout)

    def jobs(self):
        directory = os.path.join(self.home, "jobs")
        return os.listdir(directory) if os.path.isdir(directory) else []

    def make_project(self):
        """项目 p1（显示名 产品线A）：两个工程 app / web；角色 dev（专属，负责 app）、qa（覆盖全局，负责两个）。"""
        self.web_dir = os.path.join(self.tmp, "web-repo")
        os.makedirs(self.web_dir)
        os.makedirs(os.path.join(self.web_dir, "src"))
        self.ok("projects", "add", "p1", "--name", "产品线A", "--repo", f"app={self.work}")
        self.ok("projects", "add-repo", "p1", f"web={self.web_dir}", "--channels", "c1,c2")
        self.ok("roles", "add", "dev", "-P", "p1", "--title", "后端开发工程师", "--repos", "app", "--backend", "fakepi")
        os.makedirs(os.path.join(self.home, "roles"))
        with open(os.path.join(self.home, "roles", "qa.json"), "w") as f:
            json.dump({"title": "测试工程师", "backend": "fakepi", "channels": "all", "statuses": ["PASS", "FAIL"],
                       "gates": [{"type": "status_reported"}, {"type": "run_dir", "when": "run_dir"}], "runs_root": "runs"}, f)
        with open(os.path.join(self.home, "roles", "qa.brief.md"), "w") as f:
            f.write("你是{{role}}，在 {{project}} / {{repo}} 干活，通道 {{channels}}\n{{task}}\n")
        self.ok("roles", "add", "qa", "-P", "p1", "--repos", "app,web")


    def make_pool(self, role, *pairs):
        for alias, model_id in (("fa", "m-a"), ("fb", "m-b")):
            if alias not in self.ok("models").stdout:
                self.ok("models", "add", alias, "--backend", "fakepi", "--id", model_id)
        self.ok("roles", "pool", role, "-P", "p1", *pairs)


    def start_web(self):
        """起一个临时网页服务，返回端口；tearDown 的 stop 会停掉它。"""
        import socket
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = str(sock.getsockname()[1])
        self.ok("web", "start", "--port", port)
        return port


class DispatchTest(DispatchBase):
    def test_start_wait_gate_accept(self):
        self.ok("start", "j1", "-p", "t", "--batch", "b1", "MKRUN r1\nSTATUS: PASS\nRUN_DIR: runs/r1")
        self.ok("wait", "j1", "--timeout", "20")
        job = self.job("j1")
        self.assertEqual((job["state"], job["status"], job["batch"]), ("done", "PASS", "b1"))
        self.assertEqual(job["report"]["RUN_DIR"], "runs/r1")
        self.assertRegex(job["session_id"], r"^ad-j1-[0-9a-f]{6}$")
        # 没跑闸门不能验收
        self.assertEqual(self.cli("accept", "j1").returncode, 2)
        gate = self.ok("gate", "j1")
        self.assertIn("通过", gate.stdout)
        self.ok("accept", "j1")
        self.assertEqual(self.job("j1")["state"], "accepted")
        history = self.ok("history", "j1").stdout
        self.assertIn("工具 bash", history)
        self.assertIn("echo hi", history)
        self.assertIn("STATUS: PASS", self.ok("log", "j1").stdout)

    def test_gate_fails_without_report_and_status(self):
        self.ok("start", "j2", "-p", "t", "只干活不汇报")
        self.ok("wait", "j2", "--timeout", "20")
        gate = self.cli("gate", "j2")
        self.assertEqual(gate.returncode, 1)
        self.assertIn("✗ 结论含 STATUS", gate.stdout)
        self.assertIn("✗ run 目录存在", gate.stdout)
        self.assertEqual(self.cli("accept", "j2").returncode, 2)
        # 强制验收必须写原因
        self.assertEqual(self.cli("accept", "j2", "--force").returncode, 2)
        self.ok("accept", "j2", "--force", "--note", "人工确认过")

    def test_report_with_chinese_field_names(self):
        sys.path.insert(0, ROOT)
        from agent_dispatch.runner import parse_report
        status, fields = parse_report("做完了。\n状态：通过\n总结：接口都加上了\n证据：db6939c, web.py\n提问：")
        self.assertEqual((status, fields["SUMMARY"], fields["EVIDENCE"]), ("PASS", "接口都加上了", "db6939c, web.py"))
        self.assertEqual(parse_report("状态：需要决定\n问题：要不要清库")[0], "NEEDS_DECISION")
        self.assertIsNone(parse_report("当前状态：还在跑")[0])  # 不是独占一行的字段，不算

    def test_template_echo_is_not_a_status(self):
        self.ok("start", "j3", "-p", "t", "STATUS: PASS | FAIL | BLOCKED")
        self.ok("wait", "j3", "--timeout", "20")
        self.assertIsNone(self.job("j3")["status"])

    def test_verdicts_gate(self):
        run_dir = os.path.join(self.work, "runs", "r4")
        self.ok("start", "j4", "-p", "t", "MKRUN r4\nSTATUS: PASS\nRUN_DIR: runs/r4")
        self.ok("wait", "j4", "--timeout", "20")
        with open(os.path.join(run_dir, "x.playbook.md"), "w") as f:
            f.write("---\nflow: x\ncovers:\n  - E/TC-1\n  - E/TC-2\nverified-at: 2026-01-01\n---\n正文\n")
        gate = self.cli("gate", "j4")
        self.assertEqual(gate.returncode, 1)
        self.assertIn("剧本声明了 2 个用例但没有回填判定", gate.stdout)
        with open(os.path.join(run_dir, "verdicts.json"), "w") as f:
            json.dump({"verdicts": [{"tc": "E/TC-1", "verdict": "FAIL", "evidence": "report.md#a"}]}, f)
        gate = self.ok("gate", "j4")
        self.assertIn("未回填：E/TC-2", gate.stdout)
        self.assertIn("自报 PASS，但这些用例不是 PASS：E/TC-1", gate.stdout)

    def test_reply_continues_same_session(self):
        self.ok("start", "j5", "-p", "t", "-b", "fakeoc", "STATUS: NEEDS_DECISION\nQUESTION: 清不清库")
        self.ok("wait", "j5", "--timeout", "20")
        job = self.job("j5")
        self.assertEqual((job["status"], job["session_id"]), ("NEEDS_DECISION", "ses_fake123"))
        self.assertEqual(job["report"]["QUESTION"], "清不清库")
        self.ok("escalate", "j5", "要不要清库")
        self.assertIn("待你决定", self.ok("status").stdout)
        self.ok("reply", "j5", "不清库，继续\nSTATUS: PASS")
        self.ok("wait", "j5", "--timeout", "20")
        job = self.job("j5")
        self.assertEqual((job["turn"], job["status"], job["state"]), (2, "PASS", "done"))
        self.assertIsNone(job["needs_user"])
        # 假智能体按会话计数：第 2 轮说明续上了同一个会话
        with open(os.path.join(self.home, "jobs", "j5", "result.md")) as f:
            result = f.read()
        self.assertIn("第 2 轮", result)

    def test_channels_are_exclusive_and_released(self):
        self.ok("start", "j6", "-p", "t", "SLEEP 30")
        busy = self.cli("start", "j7", "-p", "t", "x")
        self.assertEqual(busy.returncode, 2)
        self.assertIn("被 j6 占用", busy.stderr)
        self.assertNotIn("j7", self.jobs())
        timed_out = self.cli("wait", "j6", "--timeout", "1")
        self.assertEqual(timed_out.returncode, 3)
        self.ok("stop", "j6", "--reason", "测试")
        self.assertEqual(self.job("j6")["state"], "stopped")
        self.ok("start", "j7", "-p", "t", "--channels", "a", "SLEEP 30")
        self.ok("start", "j8", "-p", "t", "--channels", "b", "STATUS: PASS")
        self.ok("wait", "j8", "--timeout", "20")

    def test_take_and_release(self):
        self.ok("start", "j9", "-p", "t", "STATUS: FAIL")
        self.ok("wait", "j9", "--timeout", "20")
        taken = self.ok("take", "j9")
        self.assertIn("fakepi --session-id ad-j9", taken.stdout)
        self.assertEqual(self.cli("reply", "j9", "继续").returncode, 2)
        self.assertEqual(self.cli("start", "j10", "-p", "t", "x").returncode, 2)  # 通道归人工
        self.ok("release", "j9")
        self.assertEqual(self.job("j9")["state"], "done")
        self.ok("start", "j10", "-p", "t", "STATUS: PASS")

    def test_failures_settle(self):
        self.ok("start", "j11", "-p", "t", "EXIT 1")
        self.ok("start", "j12", "-p", "t", "--channels", "none", "SILENT")
        self.ok("start", "j13", "-p", "t", "--channels", "none", "-b", "missing", "x")
        self.ok("wait", "j11", "j12", "j13", "--timeout", "20")
        self.assertEqual(self.job("j11")["state"], "failed")
        self.assertIn("没有给出任何回复", self.job("j12")["reason"])
        self.assertIn("运行器出错", self.job("j13")["reason"])

    def test_timeout_kills_worker(self):
        self.ok("start", "j14", "--cwd", self.work, "-b", "fakepi", "--timeout", "0.02", "SLEEP 30")
        self.ok("wait", "j14", "--timeout", "20")
        job = self.job("j14")
        self.assertEqual(job["state"], "failed")
        self.assertIn("被终止", job["reason"])

    @unittest.skipUnless(shutil.which("tmux"), "需要 tmux")
    def test_tmux_mode_keeps_session_alive_between_turns(self):
        self.ok("start", "t1", "-p", "t", "--mode", "tmux", "MKRUN x STATUS: NEEDS_DECISION")
        self.ok("wait", "t1", "--timeout", "30")
        job = self.job("t1")
        self.assertEqual((job["state"], job["mode"], job["tmux"]), ("done", "tmux", "ad-t1"))
        # 派工单是多行的：整段作为一条提示送达，而不是被拆成多条
        self.assertEqual(subprocess.run(["tmux", "has-session", "-t", "=ad-t1"], capture_output=True).returncode, 0)
        # 回合结束后界面还活着，通道仍被占用
        busy = self.cli("start", "t2", "-p", "t", "x")
        self.assertEqual(busy.returncode, 2)
        self.assertIn("被 t1 占用", busy.stderr)
        self.ok("reply", "t1", "STATUS: PASS")
        self.ok("wait", "t1", "--timeout", "30")
        self.assertIn("✓ 回合正常结束", self.cli("gate", "t1").stdout)  # tmux 方式没有退出码，不能因此卡闸门
        job = self.job("t1")
        self.assertEqual((job["turn"], job["status"], job["state"]), (2, "PASS", "done"))
        with open(os.path.join(self.home, "jobs", "t1", "result.md")) as f:
            self.assertIn("第 2 轮", f.read())  # 同一个进程接着干，不是重启
        history = self.ok("history", "t1").stdout
        self.assertEqual(history.count("工具 bash"), 2)
        # 界面实际用的模型与指定的不同：要被发现并记下来
        self.assertEqual(job["model_actual"], "fake/other")
        self.assertIn("实际使用的模型是 fake/other", self.ok("log", "t1").stdout)
        # 驳回后关闭界面并释放通道
        self.ok("reject", "t1", "--reason", "测试")
        self.assertNotEqual(subprocess.run(["tmux", "has-session", "-t", "=ad-t1"], capture_output=True).returncode, 0)
        self.ok("start", "t2", "-p", "t", "STATUS: PASS")

    @unittest.skipUnless(shutil.which("tmux"), "需要 tmux")
    def test_tmux_job_window_is_linked_into_hub(self):
        def hub_windows():
            out = subprocess.run(["tmux", "list-windows", "-t", "=" + self.hub, "-F", "#{window_name}"],
                                 capture_output=True, text=True)
            return out.stdout.split()
        started = self.ok("start", "h1", "-p", "t", "--mode", "tmux", "STATUS: PASS")
        self.assertIn("agent-dispatch hub", started.stderr + started.stdout)
        self.ok("wait", "h1", "--timeout", "30")
        self.assertEqual(hub_windows(), ["总览", "h1"])  # 派出时自动挂进汇总会话，窗口以作业名命名
        # 没有终端时只给出接入命令；重复执行不会把同一个窗口挂两次
        self.assertIn("tmux attach -t " + self.hub, self.ok("hub").stdout)
        self.assertEqual(hub_windows(), ["总览", "h1"])
        # 汇总会话被关掉不影响作业；再执行 hub 会重建并把还开着的界面补挂回来
        subprocess.run(["tmux", "kill-session", "-t", "=" + self.hub], capture_output=True)
        self.assertEqual(subprocess.run(["tmux", "has-session", "-t", "=ad-h1"], capture_output=True).returncode, 0)
        self.ok("hub")
        self.assertEqual(hub_windows(), ["总览", "h1"])
        # 驳回后界面关闭：窗口要从汇总会话里一起消失，不能留下还在跑的执行者
        self.ok("reject", "h1", "--reason", "测试")
        self.assertNotEqual(subprocess.run(["tmux", "has-session", "-t", "=ad-h1"], capture_output=True).returncode, 0)
        self.assertEqual(hub_windows(), ["总览"])
        # 关掉汇总会话的开关后照常派工，只是不挂
        self.ok("start", "h2", "-p", "t", "--mode", "tmux", "STATUS: PASS", env_extra={"AGENT_DISPATCH_HUB": ""})
        self.ok("wait", "h2", "--timeout", "30")
        self.assertEqual(hub_windows(), ["总览"])

    @unittest.skipUnless(shutil.which("tmux"), "需要 tmux")
    def test_tmux_mode_take_while_running_and_stop(self):
        self.ok("start", "t3", "-p", "t", "--mode", "tmux", "SLEEP 4\nSTATUS: PASS")
        taken = self.ok("take", "t3")  # 运行中也能接手，不需要先叫停
        self.assertIn("tmux attach -t ad-t3", taken.stdout)
        self.assertEqual(self.job("t3")["state"], "held")
        self.assertEqual(self.cli("reply", "t3", "x").returncode, 2)
        import time
        time.sleep(8)
        job = self.job("t3")  # 回合结束不抢走人工持有状态
        self.assertEqual((job["state"], job["prev_state"], job["status"]), ("held", "done", "PASS"))
        self.ok("release", "t3")
        self.assertEqual(self.job("t3")["state"], "done")
        self.ok("start", "t4", "--cwd", self.work, "-b", "fakepi", "--mode", "tmux", "SLEEP 60")
        self.ok("stop", "t4")
        self.assertEqual(self.job("t4")["state"], "stopped")
        self.assertNotEqual(subprocess.run(["tmux", "has-session", "-t", "=ad-t4"], capture_output=True).returncode, 0)
        self.ok("stop", "t3")  # 已结束的作业：stop 只关界面

    @unittest.skipUnless(shutil.which("tmux"), "需要 tmux")
    def test_follow_reattaches_to_running_session(self):
        self.ok("start", "t5", "-p", "t", "--mode", "tmux", "SLEEP 6\nSTATUS: PASS")
        import signal, time
        time.sleep(1.5)
        os.kill(self.job("t5")["runner_pid"], signal.SIGKILL)  # 监视进程没了，界面还在跑
        time.sleep(0.5)
        self.assertEqual(self.job("t5")["state"], "interrupted")
        self.ok("follow", "t5")
        self.ok("wait", "t5", "--timeout", "30")
        job = self.job("t5")
        self.assertEqual((job["state"], job["status"]), ("done", "PASS"))

    def test_worker_sees_job_cwd_in_pwd(self):
        self.ok("start", "j16", "-p", "t", "PWDCHECK\nSTATUS: PASS")
        self.ok("wait", "j16", "--timeout", "20")
        self.assertEqual(self.job("j16")["report"]["PWD_OK"], "yes")

    def test_worker_cannot_dispatch_further(self):
        self.ok("start", "j17", "-p", "t", "NESTED\nSTATUS: PASS")
        self.ok("wait", "j17", "--timeout", "20")
        self.assertEqual(self.job("j17")["report"]["NESTED_RC"], "2")
        self.assertNotIn("nested", self.jobs())

    def test_web_runs_in_background(self):
        import socket, urllib.request
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = str(sock.getsockname()[1])
        try:
            started = self.ok("web", "start", "--port", port)
            self.assertIn(f"http://127.0.0.1:{port}/", started.stderr)
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/overview", timeout=5) as response:
                self.assertIn("jobs", json.load(response))
            self.assertIn("已经在运行", self.ok("web", "start", "--port", port).stderr)
            self.assertIn("在运行", self.ok("web", "status").stdout)
        finally:
            self.ok("web", "stop")
        self.assertIn("没有在运行", self.ok("web", "status").stdout)

    def test_project_role_dispatch(self):
        self.make_project()
        listing = self.ok("roles", "-P", "p1").stdout
        self.assertIn("后端开发工程师", listing)
        self.assertIn("项目覆盖全局", listing)
        self.ok("start", "r1", "-P", "p1", "-r", "后端开发工程师", "STATUS: PASS")  # 用称呼指角色
        self.ok("wait", "r1", "--timeout", "20")
        job = self.job("r1")
        self.assertEqual((job["role"], job["role_title"], job["group"], job["project"], job["repo"]),
                         ("dev", "后端开发工程师", "产品线A", os.path.basename(self.work), "app"))
        self.assertEqual((job["cwd"], job["profile"], job["channels"]), (self.work, "p1:dev@app", []))
        with open(os.path.join(self.home, "jobs", "r1", "brief.md")) as f:
            self.assertIn("你是一名**后端开发工程师**", f.read())
        self.assertIn("后端开发工程师", self.ok("status").stdout)
        self.ok("gate", "r1")  # 闸门按角色定义重新解析

    def test_role_spanning_repos_needs_repo_or_detection(self):
        self.make_project()
        ambiguous = self.cli("start", "r2", "-P", "p1", "-r", "qa", "x")
        self.assertEqual(ambiguous.returncode, 2)
        self.assertIn("--repo", ambiguous.stderr)
        # 在工程目录（含子目录）里派工：项目和工程都自动识别
        brief = self.ok("start", "r2", "-r", "qa", "--dry-run", "跑一下", cwd=os.path.join(self.web_dir, "src")).stdout
        self.assertIn(f"你是测试工程师，在 产品线A / {os.path.basename(self.web_dir)} 干活，通道 c1、c2", brief)
        # 角色不负责的工程不能派
        self.assertEqual(self.cli("start", "r3", "-P", "p1", "-r", "dev", "--repo", "web", "x").returncode, 2)
        # 同一工程的通道被所有角色共用
        self.ok("start", "r4", "-P", "p1", "-r", "qa", "--repo", "web", "SLEEP 30")
        busy = self.cli("start", "r5", "-P", "p1", "-r", "qa", "--repo", "web", "x")
        self.assertIn("被 r4 占用", busy.stderr)
        self.ok("start", "r6", "-P", "p1", "-r", "qa", "--repo", "app", "STATUS: PASS")  # 另一个工程不受影响
        self.ok("wait", "r6", "--timeout", "20")
        gate = self.ok("gate", "r6").stdout  # 没有 run 目录：相关闸门跳过而不是失败
        self.assertIn("已跳过", gate)

    def test_global_role_outside_any_project(self):
        self.make_project()
        outside = os.path.join(self.tmp, "elsewhere")
        os.makedirs(outside)
        started = self.ok("start", "g1", "-r", "qa", "--cwd", outside, "--channels", "none", "STATUS: PASS")
        self.assertNotIn("Traceback", started.stderr)
        self.assertIn("角色=测试工程师 工程=elsewhere", started.stderr)
        self.ok("wait", "g1", "--timeout", "20")
        job = self.job("g1")
        self.assertEqual((job["role"], job["project"], job["project_id"]), ("qa", "elsewhere", None))

    def test_model_pool_strict_ratio(self):
        self.make_project()
        self.make_pool("dev", "fa=2", "fb=1")
        rejected = self.cli("start", "x0", "-P", "p1", "-r", "dev", "--model", "m-a", "x")
        self.assertEqual(rejected.returncode, 2)
        self.assertIn("模型池", rejected.stderr)
        picks = []
        for i in range(6):
            self.ok("start", f"p{i}", "-P", "p1", "-r", "dev", "STATUS: PASS")
            self.ok("wait", f"p{i}", "--timeout", "20")
            picks.append(self.job(f"p{i}")["model_alias"])
        self.assertEqual(picks, ["fa", "fb", "fa", "fa", "fb", "fa"])  # 2:1 严格配比
        self.assertEqual(self.job("p0")["model"], "m-a")
        # 停用的模型自动跳过
        self.ok("models", "disable", "fa")
        self.ok("start", "p6", "-P", "p1", "-r", "dev", "STATUS: PASS")
        self.assertEqual(self.job("p6")["model_alias"], "fb")
        self.ok("wait", "p6", "--timeout", "20")
        self.ok("models", "disable", "fb")
        none_left = self.cli("start", "p7", "-P", "p1", "-r", "dev", "x")
        self.assertIn("没有可用的模型", none_left.stderr)
        self.assertIn("fa=2", self.ok("roles", "-P", "p1").stdout)

    def test_model_pool_priority_tiers(self):
        self.make_project()
        self.make_pool("dev", "fa=1", "fb=1@2")
        self.assertIn("fb=1@2", self.ok("roles", "-P", "p1").stdout)
        self.assertIn("p1:dev=1@2", self.ok("models").stdout)
        for i in range(3):  # 高优先级能用时，一直用它
            self.ok("start", f"t{i}", "-P", "p1", "-r", "dev", "STATUS: PASS")
            self.ok("wait", f"t{i}", "--timeout", "20")
            self.assertEqual(self.job(f"t{i}")["model_alias"], "fa")
        # 高优先级并发满了：落到下一档，而不是排队
        self.ok("models", "limit", "fa", "1")
        self.ok("start", "t3", "-P", "p1", "-r", "dev", "SLEEP 3\nSTATUS: PASS")
        self.assertEqual(self.job("t3")["model_alias"], "fa")
        self.ok("start", "t4", "-P", "p1", "-r", "dev", "STATUS: PASS")
        self.assertEqual((self.job("t4")["model_alias"], self.job("t4")["state"] != "queued"), ("fb", True))
        self.ok("wait", "t3", "--timeout", "30")
        self.ok("wait", "t4", "--timeout", "30")
        # 高优先级空出来后回到它；停用后才用下一档
        self.ok("start", "t5", "-P", "p1", "-r", "dev", "STATUS: PASS")
        self.assertEqual(self.job("t5")["model_alias"], "fa")
        self.ok("wait", "t5", "--timeout", "20")
        self.ok("models", "disable", "fa")
        self.ok("start", "t6", "-P", "p1", "-r", "dev", "STATUS: PASS")
        self.assertEqual(self.job("t6")["model_alias"], "fb")
        self.ok("wait", "t6", "--timeout", "20")
        for bad in ("fb=1@0", "fb=1@x", "fb=1@1.5"):
            self.assertNotEqual(self.cli("roles", "pool", "dev", "-P", "p1", "fa=1", bad).returncode, 0)

    def test_model_pool_same_tier_balances_by_running_jobs(self):
        self.make_project()
        self.make_pool("dev", "fa=1", "fb=1")
        # 先把历史派工数拉偏：fb 停用期间 fa 连派 3 张
        self.ok("models", "disable", "fb")
        for i in range(3):
            self.ok("start", f"h{i}", "-P", "p1", "-r", "dev", "STATUS: PASS")
            self.ok("wait", f"h{i}", "--timeout", "20")
        self.ok("models", "enable", "fb")
        # 都空闲时按历史配比补给 fb；fb 在跑时下一张给空闲的 fa，而不是继续补派 fb
        self.ok("start", "b1", "-P", "p1", "-r", "dev", "SLEEP 4\nSTATUS: PASS")
        self.assertEqual(self.job("b1")["model_alias"], "fb")
        self.ok("start", "b2", "-P", "p1", "-r", "dev", "SLEEP 4\nSTATUS: PASS")
        self.assertEqual(self.job("b2")["model_alias"], "fa")
        self.ok("start", "b3", "-P", "p1", "-r", "dev", "STATUS: PASS")  # 各跑 1 张打平：回到历史配比，补 fb
        self.assertEqual(self.job("b3")["model_alias"], "fb")
        for name in ("b1", "b2", "b3"):
            self.ok("wait", name, "--timeout", "30")

    def test_provider_error_fails_over_to_another_model(self):
        self.make_project()
        self.make_pool("dev", "fa=1", "fb=1")
        self.ok("start", "f1", "-P", "p1", "-r", "dev", "FAILON m-a\nSTATUS: PASS")
        self.ok("wait", "f1", "--timeout", "30")
        job = self.job("f1")
        self.assertEqual((job["state"], job["status"], job["model_alias"]), ("done", "PASS", "fb"))
        self.assertEqual(job["model_attempts"][0]["model"], "fa")
        self.assertIn("quota", job["model_attempts"][0]["reason"])
        self.assertIn("暂停", self.ok("models").stdout)  # 出错的模型被暂停
        log = self.ok("log", "f1", "-n", "100").stdout
        self.assertIn("自动换成模型 fb", log)
        # 暂停期间新作业不会选到它
        self.ok("start", "f2", "-P", "p1", "-r", "dev", "STATUS: PASS")
        self.assertEqual(self.job("f2")["model_alias"], "fb")
        self.ok("wait", "f2", "--timeout", "20")
        self.ok("models", "resume", "fa")
        # 干到一半才被拒：只暂停，不自动重派
        self.ok("start", "f3", "-P", "p1", "-r", "dev", "FAILAFTERTOOL m-a\nFAILAFTERTOOL m-b\nSTATUS: PASS")
        self.ok("wait", "f3", "--timeout", "30")
        job = self.job("f3")
        self.assertEqual((job["state"], job["model_attempts"]), ("failed", []))
        self.assertIn("不自动换模型重派", self.ok("log", "f3", "-n", "100").stdout)

    def test_global_role_pool_and_project_override(self):
        self.make_project()
        self.make_pool("dev", "fa=1")  # 先登记模型；dev 是项目专属角色
        self.ok("roles", "pool", "dev", "-P", "p1", "--clear")
        # qa 是全局角色：在全局配模型池，项目里没配的就沿用它
        self.ok("roles", "pool", "qa", "--global", "fb=1")
        self.assertIn("fb=1", self.ok("roles", "--global").stdout)
        self.ok("start", "q1", "-P", "p1", "-r", "qa", "--repo", "app", "STATUS: PASS")
        self.assertEqual(self.job("q1")["model_alias"], "fb")
        self.ok("wait", "q1", "--timeout", "20")
        # 项目单独配置的模型池覆盖全局的
        self.ok("roles", "pool", "qa", "-P", "p1", "fa=1")
        self.ok("start", "q2", "-P", "p1", "-r", "qa", "--repo", "app", "STATUS: PASS")
        self.assertEqual(self.job("q2")["model_alias"], "fa")
        self.ok("wait", "q2", "--timeout", "20")
        # 全局覆盖写在本机目录里，随工具发布的角色文件不被改动
        with open(os.path.join(self.home, "roles", "qa.json")) as f:
            self.assertEqual(json.load(f)["model_pool"], [{"model": "fb", "weight": 1}])

    def test_disabled_backend_is_not_used(self):
        self.make_project()
        self.make_pool("dev", "fa=1", "fb=1")
        self.ok("backends", "disable", "fakepi")
        self.assertIn("已停用", self.ok("backends").stdout)
        refused = self.cli("start", "b1", "-P", "p1", "-r", "dev", "x")
        self.assertIn("没有可用的模型", refused.stderr)
        self.assertIn("派工工具 fakepi 已停用", refused.stderr)
        plain = self.cli("start", "b2", "--cwd", self.work, "-b", "fakepi", "x")  # 不走模型池的派工同样被拦
        self.assertIn("已停用", plain.stderr)
        self.assertIn("fakepi 已停用", self.ok("models").stdout)
        self.ok("backends", "enable", "fakepi")
        self.ok("start", "b3", "--cwd", self.work, "-b", "fakepi", "--channels", "none", "STATUS: PASS")
        self.ok("wait", "b3", "--timeout", "20")

    def test_supervisor_role_doc_includes_project(self):
        self.make_project()
        with open(os.path.join(self.home, "projects", "p1", "supervisor.md"), "w") as f:
            f.write("本项目的专属规则：先问再清库\n")
        doc = self.ok("role", cwd=self.work).stdout  # 在工程目录里：自动带上项目
        self.assertIn("# 主管角色", doc)
        self.assertIn("本项目的专属规则：先问再清库", doc)
        self.assertIn("后端开发工程师", doc)
        self.assertNotIn("产品线A", self.ok("role", cwd=self.tmp).stdout)
        self.assertIn("p1", self.ok("projects").stdout)
        self.assertIn(self.web_dir, self.ok("projects", "show", "产品线A").stdout)

    def test_entry_script_works_through_symlink(self):
        """命令入口经软链接调用（如 ~/.local/bin/agent-dispatch）也要能找到工程目录。"""
        link_dir = os.path.join(self.tmp, "bin")
        os.makedirs(link_dir)
        os.symlink(os.path.join(ROOT, "bin", "agent-dispatch"), os.path.join(link_dir, "agent-dispatch"))
        env = dict(os.environ, AGENT_DISPATCH_HOME=self.home, AGENT_DISPATCH_BACKENDS=self.backends)
        env.pop("PYTHONPATH", None)
        result = subprocess.run([os.path.join(link_dir, "agent-dispatch"), "status"], env=env, cwd=self.tmp,
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("暂无作业", result.stdout)

    def test_web_edits_models_and_pools(self):
        import socket, urllib.error, urllib.request
        self.make_project()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        base = f"http://127.0.0.1:{port}"

        def call(method, path, body=None, header=True):
            request = urllib.request.Request(base + path, method=method, data=json.dumps(body or {}).encode())
            request.add_header("Content-Type", "application/json")
            if header:
                request.add_header("X-Agent-Dispatch", "1")
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    return response.status, json.load(response)
            except urllib.error.HTTPError as exc:
                return exc.code, json.load(exc)

        try:
            self.ok("web", "start", "--port", str(port))
            self.assertEqual(call("POST", "/api/models", {"alias": "fa", "backend": "fakepi", "id": "m-a"}, header=False)[0], 403)
            self.assertEqual(call("POST", "/api/models", {"alias": "fa", "backend": "fakepi", "id": "m-a"})[0], 200)
            self.assertEqual(call("POST", "/api/models", {"alias": "fb", "backend": "fakepi", "id": "m-b"})[0], 200)
            status, error = call("POST", "/api/pools", {"project": "p1", "role": "dev", "pool": [{"model": "nope", "weight": 1}]})
            self.assertEqual(status, 400)
            self.assertIn("不在模型登记表里", error["error"])
            self.assertEqual(call("POST", "/api/pools", {"project": "p1", "role": "dev",
                                                         "pool": [{"model": "fa", "weight": 3}, {"model": "fb", "weight": 1}]})[0], 200)
            self.assertEqual(call("POST", "/api/models/fb", {"enabled": False})[0], 200)
            page = call("GET", "/api/models")[1]
            dev = next(r for r in page["roles"] if r["role"] == "dev")
            self.assertEqual(dev["pool"], [{"model": "fa", "weight": 3}, {"model": "fb", "weight": 1}])
            self.assertFalse(next(m for m in page["models"] if m["alias"] == "fb")["enabled"])
            self.assertEqual(call("DELETE", "/api/models/fa")[0], 400)  # 还在模型池里，不能删
            self.ok("start", "w1", "-P", "p1", "-r", "dev", "STATUS: PASS")  # 网页上的配置即时生效
            self.assertEqual(self.job("w1")["model_alias"], "fa")
            self.ok("wait", "w1", "--timeout", "20")
        finally:
            self.ok("web", "stop")

    def test_worker_cannot_stop_users_web_service(self):
        env = dict(os.environ, AGENT_DISPATCH_WORKER="j-x", PYTHONPATH=ROOT)
        env.pop("AGENT_DISPATCH_HOME", None)
        result = subprocess.run([sys.executable, "-m", "agent_dispatch", "web", "stop"], env=env,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 2)
        self.assertIn("不能启停用户的网页服务", result.stderr)

    def test_web_dispatches_jobs(self):
        """网页的操作接口：派工、续派、叫停、裁决都走子进程命令行。"""
        import socket, urllib.error, urllib.request
        self.make_project()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        base = f"http://127.0.0.1:{port}"

        def call(method, path, body=None, header=True):
            request = urllib.request.Request(base + path, method=method, data=json.dumps(body or {}).encode())
            request.add_header("Content-Type", "application/json")
            if header:
                request.add_header("X-Agent-Dispatch", "1")
            try:
                with urllib.request.urlopen(request, timeout=70) as response:
                    return response.status, json.load(response)
            except urllib.error.HTTPError as exc:
                try:
                    return exc.code, json.load(exc)
                finally:
                    exc.close()

        try:
            self.ok("web", "start", "--port", str(port))
            # 没有请求头的写操作被拒
            self.assertEqual(call("POST", "/api/jobs", {"project": "p1", "role": "dev", "task": "x"}, header=False)[0], 403)
            # 派工选项里能看到项目 p1 和它的角色
            options = call("GET", "/api/dispatch-options")[1]
            p1 = next(p for p in options["projects"] if p["id"] == "p1")
            self.assertEqual(p1["name"], "产品线A")
            self.assertTrue({"dev", "qa"} <= {r["id"] for r in p1["roles"]})
            dev = next(r for r in p1["roles"] if r["id"] == "dev")
            self.assertEqual(dev["repos"], ["app"])
            # 派一个角色不存在的作业：400 且错误信息里含「没有角色」
            status, error = call("POST", "/api/jobs", {"job": "w-nope", "project": "p1", "role": "nope", "task": "x"})
            self.assertEqual(status, 400)
            self.assertIn("没有角色", error["error"])
            # 派工并等它跑完
            self.assertEqual(call("POST", "/api/jobs", {"job": "w2", "project": "p1", "role": "dev",
                                                          "task": "STATUS: PASS"})[0], 200)
            self.ok("wait", "w2", "--timeout", "20")
            self.assertEqual(self.job("w2")["state"], "done")
            # 不填作业名：自动生成，以角色 ID 开头
            status, started = call("POST", "/api/jobs", {"project": "p1", "role": "dev", "task": "STATUS: PASS"})
            self.assertEqual(status, 200)
            self.assertTrue(started["job"].startswith("dev-"))
            self.ok("wait", started["job"], "--timeout", "20")
            # 待你决定在总览里给问题原文（不是 {question: …} 对象），新界面的工作台直接显示它
            self.ok("escalate", "w2", "要不要清库")
            overview = call("GET", "/api/overview")[1]
            w2 = next(j for j in overview["jobs"] if j["name"] == "w2")
            self.assertEqual(w2["needs_user"], "要不要清库")
            self.assertEqual(call("POST", "/api/jobs/w2/reply", {"text": "不清库，继续\nSTATUS: PASS"})[0], 200)
            self.ok("wait", "w2", "--timeout", "20")
            self.assertIsNone(self.job("w2")["needs_user"])
            # 依次闸门、验收；驳回不带 reason 返回 400
            self.assertEqual(call("POST", "/api/jobs/w2/gate")[1]["passed"], True)
            self.assertEqual(call("POST", "/api/jobs/w2/accept")[0], 200)
            self.assertEqual(call("POST", "/api/jobs/w2/reject", {"reason": ""})[0], 400)
            # 长任务可以叫停
            self.assertEqual(call("POST", "/api/jobs", {"job": "w3", "project": "p1", "role": "dev",
                                                          "task": "SLEEP 30"})[0], 200)
            status, stopped = call("POST", "/api/jobs/w3/stop", {"reason": "测试叫停"})
            self.assertEqual(status, 200)
            self.assertEqual(self.job("w3")["state"], "stopped")
            # 续派和清除待决标记
            self.assertEqual(call("POST", "/api/jobs/w3/reply", {"text": "STATUS: PASS"})[0], 200)
            self.ok("wait", "w3", "--timeout", "20")
            self.assertEqual(call("POST", "/api/jobs/w3/clear-escalation")[0], 200)
        finally:
            self.ok("web", "stop")

    def test_dry_run_prints_brief(self):
        out = self.ok("start", "j15", "-p", "t", "--dry-run", "跑场景A").stdout
        self.assertIn("派工单 j15 通道 a、b", out)
        self.assertIn("跑场景A", out)
        self.assertEqual(self.jobs(), [])

    def test_dry_run_json_preview(self):
        """start --dry-run --json：不派出，给出模型池选中的模型和通道占用情况。"""
        self.make_project()
        self.make_pool("dev", "fa=2", "fb=1")
        # 通道被 r4 占着：预览要列出占用者，且自己不创建作业
        self.ok("start", "r4", "-P", "p1", "-r", "qa", "--repo", "web", "SLEEP 30")
        try:
            result = self.ok("start", "r7", "-P", "p1", "-r", "qa", "--repo", "web",
                             "--dry-run", "--json", "跑一下")
            data = json.loads(result.stdout)
            self.assertEqual(data["job"], "r7")
            self.assertEqual(data["role"], "qa")
            self.assertEqual(data["role_title"], "测试工程师")
            self.assertEqual(data["backend"], "fakepi")
            self.assertIn("你是测试工程师", data["brief"])
            self.assertEqual(set(data["channels"]), {"c1", "c2"})
            self.assertEqual(data["channels_busy"], {"c1": "r4", "c2": "r4"})
            self.assertNotIn("r7", self.jobs())
            # 模型池角色给出这次会选中的模型
            pooled = json.loads(self.ok("start", "r8", "-P", "p1", "-r", "dev",
                                         "--dry-run", "--json", "跑一下").stdout)
            self.assertEqual(pooled["model_alias"], "fa")
            self.assertIn("m-a", pooled["model"])
            self.assertNotIn("r8", self.jobs())
            # 不带 --json 的行为不变
            plain = self.ok("start", "r9", "-P", "p1", "-r", "dev", "--dry-run", "跑一下").stdout
            self.assertIn("你是", plain)
        finally:
            self.ok("stop", "r4")

    def test_web_serves_ui_dist(self):
        """有 ui_dist 时托管前端页面：前端路由回退到 index.html，静态文件给对 Content-Type，穿越被拒。"""
        import socket, urllib.error, urllib.request
        ui = os.path.join(self.tmp, "ui-dist")
        os.makedirs(os.path.join(ui, "assets"))
        with open(os.path.join(ui, "index.html"), "w") as f:
            f.write("<!doctype html><title>新界面</title>")
        with open(os.path.join(ui, "assets", "app.js"), "w") as f:
            f.write("console.log(1)")
        old_env = os.environ.get("AGENT_DISPATCH_UI_DIST")
        os.environ["AGENT_DISPATCH_UI_DIST"] = ui
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = str(sock.getsockname()[1])
        base = f"http://127.0.0.1:{port}"

        def get(path):
            request = urllib.request.Request(base + path)
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    return response.status, dict(response.headers), response.read()
            except urllib.error.HTTPError as exc:
                try:
                    return exc.code, dict(exc.headers), exc.read()
                finally:
                    exc.close()

        try:
            self.ok("web", "start", "--port", port)
            for path in ("/", "/jobs/abc"):
                status, headers, body = get(path)
                self.assertEqual(status, 200, path)
                self.assertIn("新界面", body.decode())
                self.assertIn("text/html", headers["Content-Type"])
                self.assertEqual(headers["Cache-Control"], "no-store")
            status, headers, body = get("/assets/app.js")
            self.assertEqual(status, 200)
            self.assertIn("javascript", headers["Content-Type"])
            self.assertIn("max-age", headers["Cache-Control"])
            for path in ("/assets/../../etc/passwd", "/assets/..%2f..%2fetc%2fpasswd"):
                self.assertEqual(get(path)[0], 404, path)
            # 旧页面（/legacy）已删除：只剩前端路由回退，送新界面的 index.html
            status, headers, body = get("/legacy")
            self.assertEqual(status, 200)
            self.assertIn("新界面", body.decode())
            with urllib.request.urlopen(base + "/api/overview", timeout=5) as response:
                self.assertIn("jobs", json.load(response))
        finally:
            self.ok("web", "stop")
            if old_env is None:
                del os.environ["AGENT_DISPATCH_UI_DIST"]
            else:
                os.environ["AGENT_DISPATCH_UI_DIST"] = old_env

    def test_ui_dist_is_built_and_committed(self):
        """新界面的构建产物随仓库提交：用户运行时不需要 Node，ui_dist 必须在包里。"""
        dist = os.path.join(ROOT, "agent_dispatch", "ui_dist")
        self.assertTrue(os.path.isfile(os.path.join(dist, "index.html")),
                        "缺少 ui_dist/index.html：在 ui/ 下执行 pnpm build 并提交产物")
        self.assertTrue(os.path.isdir(os.path.join(dist, "assets")), "缺少 ui_dist/assets")

    def test_web_preview_and_overview_fields(self):
        """POST /api/jobs/preview 走子进程 dry-run；总览作业带 updated / ended 字段。"""
        import socket, urllib.error, urllib.request
        self.make_project()
        port = self.start_web()
        base = f"http://127.0.0.1:{port}"

        def call(method, path, body=None, header=True):
            request = urllib.request.Request(base + path, method=method, data=json.dumps(body or {}).encode())
            request.add_header("Content-Type", "application/json")
            if header:
                request.add_header("X-Agent-Dispatch", "1")
            try:
                with urllib.request.urlopen(request, timeout=70) as response:
                    return response.status, json.load(response)
            except urllib.error.HTTPError as exc:
                try:
                    return exc.code, json.load(exc)
                finally:
                    exc.close()

        try:
            # 预览接口也要写请求头
            self.assertEqual(call("POST", "/api/jobs/preview", {"project": "p1", "role": "dev", "task": "x"},
                                  header=False)[0], 403)
            status, preview = call("POST", "/api/jobs/preview", {"project": "p1", "role": "dev", "task": "跑一下"})
            self.assertEqual(status, 200)
            self.assertIn("后端开发工程师", preview["brief"])
            self.assertEqual(preview["role"], "dev")
            self.assertNotIn("preview", self.jobs())
            status, error = call("POST", "/api/jobs/preview", {"project": "p1", "role": "nope", "task": "x"})
            self.assertEqual(status, 400)
            self.assertIn("没有角色", error["error"])
            # 总览作业补 updated / ended
            self.ok("start", "ov1", "-P", "p1", "-r", "dev", "STATUS: PASS")
            self.ok("wait", "ov1", "--timeout", "20")
            with urllib.request.urlopen(base + "/api/overview", timeout=5) as response:
                overview = json.load(response)
            job = next(j for j in overview["jobs"] if j["name"] == "ov1")
            self.assertTrue(job["updated"])
            self.assertTrue(job["ended"])
        finally:
            self.ok("web", "stop")


class ConfigApiTest(DispatchBase):
    """配置中心的纯函数与网页接口：字段来源、恢复继承、项目与主管规则的读写。"""

    SHIPPED = os.path.join(ROOT, "roles")

    def setUp(self):
        super().setUp()
        # 随工具发布的 roles/ 在测试前后必须原样
        self.shipped_snapshot = self._snapshot()

    def _snapshot(self):
        with contextlib.ExitStack() as stack:
            files = {name: stack.enter_context(open(os.path.join(self.SHIPPED, name), "rb"))
                     for name in sorted(os.listdir(self.SHIPPED))}
            return {name: f.read() for name, f in files.items()}

    def tearDown(self):
        self.assertEqual(self._snapshot(), self.shipped_snapshot, "随工具发布的 roles/ 目录被改动了")
        super().tearDown()

    def py(self, statements):
        """在子进程里用测试状态目录执行 definitions 的代码，返回 result 变量的 JSON。"""
        import textwrap
        script = ("import json, os, sys\nsys.path.insert(0, %r)\n"
                  "from agent_dispatch import definitions\n"
                  "%s\nprint('@@' + json.dumps(result, ensure_ascii=False, default=str))") % (
            ROOT, textwrap.dedent(statements))
        env = dict(os.environ, AGENT_DISPATCH_HOME=self.home, AGENT_DISPATCH_BACKENDS=self.backends, PYTHONPATH=ROOT)
        proc = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        line = [l for l in proc.stdout.splitlines() if l.startswith("@@")][-1]
        return json.loads(line[2:])

    def raises(self, statements):
        """断言子进程代码抛 DispatchError，返回错误信息。"""
        import textwrap
        script = ("import json, sys\nsys.path.insert(0, %r)\n"
                  "from agent_dispatch import definitions\nfrom agent_dispatch.store import DispatchError\n"
                  "try:\n%s\n"
                  "except DispatchError as e:\n    print('@@' + str(e))\nelse:\n    raise SystemExit('没有抛 DispatchError')") % (ROOT, textwrap.indent(textwrap.dedent(statements), "    "))
        env = dict(os.environ, AGENT_DISPATCH_HOME=self.home, AGENT_DISPATCH_BACKENDS=self.backends, PYTHONPATH=ROOT)
        proc = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        return [l for l in proc.stdout.splitlines() if l.startswith("@@")][-1][2:]

    def test_role_detail_field_sources(self):
        self.make_project()
        out = self.py("""
            project = definitions.load_project("p1")
            global_qa = definitions.role_detail(None, "qa")
            qa = definitions.role_detail(project, "qa")
            shipped = definitions.role_detail(None, "tester")
            dev = definitions.role_detail(project, "dev")
            definitions.set_role_fields(project, "qa", {"timeout_min": 30, "title": "资深测试工程师"})
            overridden = definitions.role_detail(project, "qa")
            definitions.reset_role_fields(project, "qa", ["timeout_min", "title"])
            reset = definitions.role_detail(project, "qa")
            result = {"global_qa": global_qa, "qa": qa, "shipped": shipped, "dev": dev,
                      "overridden": overridden, "reset": reset}
        """)
        # 全局角色 qa 的字段来自本机全局；项目层只覆盖了 repos
        self.assertEqual(out["global_qa"]["fields"]["backend"]["source"], "本机全局")
        self.assertEqual(out["qa"]["fields"]["backend"]["source"], "本机全局")
        self.assertEqual(out["qa"]["fields"]["repos"]["source"], "项目")
        # 随工具发布的角色：字段来源标成随工具发布
        self.assertEqual(out["shipped"]["fields"]["backend"]["source"], "随工具发布")
        self.assertEqual(out["shipped"]["title"], "测试工程师")
        # 项目专属角色：全部来自项目层
        self.assertEqual(out["dev"]["fields"]["title"]["source"], "项目")
        self.assertEqual(out["dev"]["repos"], ["app"])
        # 项目覆盖后来源变成项目；恢复继承后回落
        self.assertEqual(out["overridden"]["fields"]["timeout_min"], {"value": 30, "source": "项目"})
        self.assertEqual(out["overridden"]["fields"]["title"]["value"], "资深测试工程师")
        self.assertNotIn("timeout_min", out["reset"]["fields"])  # 本机全局层本来就没写这项，reset 后整个字段消失
        self.assertEqual(out["reset"]["fields"]["title"]["value"], "测试工程师")

    def test_project_defaults_layer(self):
        self.make_project()
        out = self.py("""
            definitions.update_project("p1", defaults={"timeout_min": 45})
            project = definitions.load_project("p1")
            result = definitions.role_detail(project, "qa")["fields"]["timeout_min"]
        """)
        self.assertEqual(out, {"value": 45, "source": "项目默认"})

    def test_set_role_fields_validation(self):
        self.make_project()
        self.assertIn("不允许", self.raises("""
            definitions.set_role_fields(definitions.load_project("p1"), "qa", {"brief_template": "x.md"})
        """))
        self.assertIn("不在项目", self.raises("""
            definitions.set_role_fields(definitions.load_project("p1"), "qa", {"repos": ["nope"]})
        """))
        self.assertIn("backends.json", self.raises("""
            definitions.set_role_fields(definitions.load_project("p1"), "qa", {"backend": "no-such-tool"})
        """))
        self.assertIn("mode", self.raises("""
            definitions.set_role_fields(definitions.load_project("p1"), "qa", {"mode": "inline"})
        """))
        # 合法写入真的落盘
        out = self.py("""
            import json as _json
            definitions.set_role_fields(definitions.load_project("p1"), "dev", {"mode": "tmux", "timeout_min": 15})
            with open(os.path.join(definitions.project_dir("p1"), "roles", "dev.json")) as f:
                result = _json.load(f)
        """)
        self.assertEqual((out["mode"], out["timeout_min"]), ("tmux", 15))

    def test_brief_write_read_reset(self):
        self.make_project()
        out = self.py("""
            project = definitions.load_project("p1")
            base = definitions.get_brief(project, "qa")
            definitions.set_brief(project, "qa", "项目层的模板")
            after_set = definitions.get_brief(project, "qa")
            definitions.reset_brief(project, "qa")
            after_reset = definitions.get_brief(project, "qa")
            global_before = definitions.get_brief(None, "tester")
            definitions.set_brief(None, "tester", "本机全局的模板")
            global_after = definitions.get_brief(None, "tester")
            definitions.reset_brief(None, "tester")
            global_reset = definitions.get_brief(None, "tester")
            result = {"base": base, "after_set": after_set, "after_reset": after_reset,
                      "global_before": global_before, "global_after": global_after, "global_reset": global_reset}
        """)
        self.assertEqual(out["base"]["source"], "本机全局")
        self.assertIn("你是{{role}}", out["base"]["text"])
        self.assertEqual((out["after_set"]["text"], out["after_set"]["source"]), ("项目层的模板", "项目"))
        self.assertEqual(out["after_reset"]["source"], "本机全局")
        self.assertEqual(out["global_before"]["source"], "随工具发布")
        self.assertEqual((out["global_after"]["text"], out["global_after"]["source"]), ("本机全局的模板", "本机全局"))
        self.assertEqual(out["global_reset"]["source"], "随工具发布")
        self.assertIn("tester", out["global_reset"]["path"])

    def test_delete_role_layer(self):
        self.make_project()
        err = self.raises("definitions.delete_role_layer(None, 'tester')")
        self.assertIn("随工具发布", err)
        self.py("""
            project = definitions.load_project("p1")
            definitions.set_role_fields(project, "qa", {"timeout_min": 30})
            definitions.delete_role_layer(project, "qa")
            import os
            result = os.path.exists(os.path.join(definitions.project_dir("p1"), "roles", "qa.json"))
        """)
        err = self.raises("definitions.delete_role_layer(definitions.load_project('p1'), 'qa')")
        self.assertIn("没有", err)

    def test_remove_repo_referenced_by_role(self):
        self.make_project()
        # web 被 qa 引用：拒绝并说明角色；app 被 dev 和 qa 同时引用
        err = self.raises("definitions.remove_repo('p1', 'web')")
        self.assertIn("qa", err)
        err = self.raises("definitions.remove_repo('p1', 'app')")
        self.assertIn("dev", err)
        self.assertIn("qa", err)
        err = self.raises("definitions.remove_repo('p1', 'nope')")
        self.assertIn("没有", err)

    def test_remove_repo_after_unreference(self):
        self.make_project()
        out = self.py("""
            project = definitions.load_project("p1")
            definitions.set_role_fields(project, "qa", {"repos": ["app"]})
            definitions.update_project("p1", name="产品线B")
            definitions.remove_repo("p1", "web")
            project = definitions.load_project("p1")
            result = {"repos": list(project["repos"]), "name": project["name"]}
        """)
        self.assertEqual(out["repos"], ["app"])
        self.assertEqual(out["name"], "产品线B")

    def test_supervisor_notes(self):
        self.make_project()
        out = self.py("""
            definitions.set_supervisor_notes("p1", "先问再清库")
            result = definitions.get_supervisor_notes("p1")
        """)
        self.assertEqual(out, "先问再清库")

    def test_brief_matches_dispatch(self):
        """配置中心看到的模板必须与派工实际用的模板是同一个文件，写入/恢复继承两边一致。"""
        self.make_project()
        out = self.py("""
            project = definitions.load_project("p1")
            # 项目层给全局角色 qa 单独改模板（这一层原本没有 qa.json）
            definitions.set_brief(project, "qa", "项目模板 v2")
            set_path = definitions.get_brief(project, "qa")
            set_resolved = definitions.resolve(project, "qa", "app")["_brief_path"]
            definitions.reset_brief(project, "qa")
            reset_path = definitions.get_brief(project, "qa")
            reset_resolved = definitions.resolve(project, "qa", "app")["_brief_path"]
            # 本机全局层覆盖随工具发布的 tester
            definitions.set_brief(None, "tester", "tester 本机模板")
            g_set = definitions.get_brief(None, "tester")
            g_set_resolved = definitions.resolve(None, "tester")["_brief_path"]
            definitions.reset_brief(None, "tester")
            g_reset = definitions.get_brief(None, "tester")
            g_reset_resolved = definitions.resolve(None, "tester")["_brief_path"]
            # 层 json 里指定了 brief_template 时，写入/读取也用它定位
            import json as _json
            definitions.set_role_fields(project, "dev", {"timeout_min": 5})
            dev_json = os.path.join(definitions.project_dir("p1"), "roles", "dev.json")
            dev = _json.load(open(dev_json)); dev["brief_template"] = "dev.custom.md"; _json.dump(dev, open(dev_json, "w"))
            definitions.set_brief(project, "dev", "dev 自定义模板")
            custom = definitions.get_brief(project, "dev")
            custom_resolved = definitions.resolve(project, "dev")["_brief_path"]
            definitions.delete_role_layer(project, "dev")
            result = {"set_path": set_path, "set_resolved": set_resolved, "reset_path": reset_path,
                      "reset_resolved": reset_resolved, "g_set": g_set, "g_set_resolved": g_set_resolved,
                      "g_reset": g_reset, "g_reset_resolved": g_reset_resolved,
                      "custom": custom, "custom_resolved": custom_resolved}
        """)
        self.assertEqual(out["set_path"]["path"], out["set_resolved"])
        self.assertEqual((out["set_path"]["text"], out["set_path"]["source"]), ("项目模板 v2", "项目"))
        self.assertEqual(out["reset_path"]["path"], out["reset_resolved"])
        self.assertEqual(out["reset_path"]["source"], "本机全局")
        self.assertEqual(out["g_set"]["path"], out["g_set_resolved"])
        self.assertEqual(out["g_set"]["text"], "tester 本机模板")
        self.assertEqual(out["g_reset"]["path"], out["g_reset_resolved"])
        self.assertEqual(out["g_reset"]["source"], "随工具发布")
        self.assertEqual(out["custom"]["path"], out["custom_resolved"])
        self.assertTrue(out["custom"]["path"].endswith("dev.custom.md"))
        self.assertEqual(out["custom"]["text"], "dev 自定义模板")

    def test_set_role_fields_unknown_role(self):
        self.make_project()
        err = self.raises("definitions.set_role_fields(definitions.load_project('p1'), 'nobody', {'timeout_min': 5})")
        self.assertIn("没有角色", err)

    def test_web_config_api(self):
        import socket, urllib.error, urllib.request
        self.make_project()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = str(sock.getsockname()[1])
        base = f"http://127.0.0.1:{port}"

        def call(method, path, body=None, header=True):
            request = urllib.request.Request(base + path, method=method, data=json.dumps(body or {}).encode())
            request.add_header("Content-Type", "application/json")
            if header:
                request.add_header("X-Agent-Dispatch", "1")
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    return response.status, json.load(response)
            except urllib.error.HTTPError as exc:
                try:
                    return exc.code, json.load(exc)
                finally:
                    exc.close()

        try:
            self.ok("web", "start", "--port", port)
            # 写操作没有请求头被拒
            self.assertEqual(call("POST", "/api/config/roles", {"id": "x"}, header=False)[0], 403)
            self.assertEqual(call("DELETE", "/api/config/roles/x", header=False)[0], 403)
            # 配置总览：项目 + 全局角色
            status, config = call("GET", "/api/config")
            self.assertEqual(status, 200)
            p1 = next(p for p in config["projects"] if p["id"] == "p1")
            self.assertEqual(p1["name"], "产品线A")
            self.assertTrue(any(r["id"] == "app" for r in p1["repos"]))
            self.assertTrue({"dev", "qa"} <= {r["id"] for r in p1["roles"]})
            self.assertIn("tester", {r["id"] for r in config["roles"]})
            # 角色详情：字段来源
            status, detail = call("GET", "/api/config/roles/qa?project=p1")
            self.assertEqual(status, 200)
            self.assertEqual(detail["fields"]["repos"]["source"], "项目")
            self.assertEqual(detail["fields"]["backend"]["source"], "本机全局")
            status, detail = call("GET", "/api/config/roles/tester")
            self.assertEqual(detail["fields"]["backend"]["source"], "随工具发布")
            # 新建角色、写字段、恢复继承
            self.assertEqual(call("POST", "/api/config/roles", {"project": "p1", "id": "ops",
                                                "title": "运维工程师", "description": "管机器"})[0], 200)
            self.assertEqual(call("POST", "/api/config/roles/ops", {"project": "p1",
                                                "fields": {"timeout_min": 20, "repos": ["app"]}})[0], 200)
            self.assertEqual(call("POST", "/api/config/roles/ops", {"project": "p1",
                                                "fields": {"repos": ["nope"]}})[0], 400)
            # 参数类型不对给 400 加中文说明，而不是 500
            status, error = call("POST", "/api/config/roles/ops", {"project": "p1", "fields": "oops"})
            self.assertEqual(status, 400)
            self.assertIn("fields", error["error"])
            status, error = call("POST", "/api/config/roles/ops", {"project": "p1", "reset": "timeout_min"})
            self.assertEqual(status, 400)
            self.assertIn("reset", error["error"])
            # 各层都不存在的角色不能凭空写字段
            status, error = call("POST", "/api/config/roles/nobody", {"project": "p1",
                                                "fields": {"timeout_min": 5}})
            self.assertEqual(status, 400)
            self.assertIn("没有角色", error["error"])
            status, detail = call("GET", "/api/config/roles/ops?project=p1")
            self.assertEqual(detail["fields"]["timeout_min"], {"value": 20, "source": "项目"})
            self.assertEqual(call("POST", "/api/config/roles/ops", {"project": "p1", "reset": ["timeout_min"]})[0], 200)
            self.assertNotIn("timeout_min", call("GET", "/api/config/roles/ops?project=p1")[1]["fields"])
            # 模板写入与恢复
            self.assertEqual(call("POST", "/api/config/roles/ops/brief", {"project": "p1", "text": "运维模板"})[0], 200)
            self.assertEqual(call("GET", "/api/config/roles/ops?project=p1")[1]["brief"]["text"], "运维模板")
            self.assertEqual(call("POST", "/api/config/roles/ops/brief", {"project": "p1", "reset": True})[0], 200)
            # 删除这一层角色
            self.assertEqual(call("DELETE", "/api/config/roles/ops?project=p1")[0], 200)
            self.assertEqual(call("GET", "/api/config/roles/ops?project=p1")[0], 404)
            # 删除随工具发布的角色被拒
            status, error = call("DELETE", "/api/config/roles/tester")
            self.assertEqual(status, 400)
            self.assertIn("随工具发布", error["error"])
            # 项目：新建、改名、增删工程、主管规则
            self.assertEqual(call("POST", "/api/config/projects", {"id": "p2", "name": "产品线C",
                                                "repos": {"srv": self.work}})[0], 200)
            self.assertEqual(call("POST", "/api/config/projects/p2", {"name": "产品线C2", "default_role": "qa"})[0], 200)
            self.assertEqual(call("POST", "/api/config/projects/p2/repos", {"id": "web2", "cwd": self.web_dir,
                                                "channels": "c3"})[0], 200)
            config = call("GET", "/api/config")[1]
            p2 = next(p for p in config["projects"] if p["id"] == "p2")
            self.assertEqual((p2["name"], p2["default_role"]), ("产品线C2", "qa"))
            self.assertEqual({r["id"] for r in p2["repos"]}, {"srv", "web2"})
            self.assertEqual(call("DELETE", "/api/config/projects/p2/repos/web2")[0], 200)
            self.assertEqual(call("POST", "/api/config/projects/p2/supervisor", {"text": "上线前要复查"})[0], 200)
            self.assertEqual(next(p for p in call("GET", "/api/config")[1]["projects"]
                                  if p["id"] == "p2")["supervisor"], "上线前要复查")
            # p1 的主管规则也能读到
            self.assertEqual(call("POST", "/api/config/projects/p1/supervisor", {"text": "p1 规则"})[0], 200)
            self.assertEqual(next(p for p in call("GET", "/api/config")[1]["projects"]
                                  if p["id"] == "p1")["supervisor"], "p1 规则")
        finally:
            self.ok("web", "stop")


class BatchTopicTest(DispatchBase):
    """事项：批次的标题/原因登记、follows 关联、batches/batch 命令与网页接口。"""

    SUP = {"AGENT_DISPATCH_SESSION": "s-batch", "AGENT_DISPATCH_SESSION_KIND": "pi"}

    def read_batch_file(self, batch):
        with open(os.path.join(self.home, "batches", f"{batch}.json"), encoding="utf-8") as f:
            return json.load(f)

    def test_batch_title_registered_and_not_cleared(self):
        self.ok("start", "bt1", "-p", "t", "--batch", "fix-01", "--batch-title", "修复登录超时",
                "--batch-reason", "用户反馈登录偶发超时", "STATUS: PASS", env_extra=self.SUP)
        data = self.read_batch_file("fix-01")
        self.assertEqual((data["id"], data["title"], data["reason"]),
                         ("fix-01", "修复登录超时", "用户反馈登录偶发超时"))
        self.assertEqual(data["dispatcher"]["session_id"], "s-batch")
        # 第二张单不带标题：不清空
        self.ok("start", "bt2", "-p", "t", "--batch", "fix-01", "--channels", "none", "SLEEP 30",
                env_extra=self.SUP)
        self.assertEqual(self.read_batch_file("fix-01")["title"], "修复登录超时")
        # 带了新值就更新
        self.ok("stop", "bt2")
        self.ok("start", "bt3", "-p", "t", "--batch", "fix-01", "--batch-title", "修复登录超时（扩展）",
                "STATUS: PASS", env_extra=self.SUP)
        self.assertEqual(self.read_batch_file("fix-01")["title"], "修复登录超时（扩展）")
        self.ok("wait", "bt3", "--timeout", "20")

    def test_batch_without_title_reminds_but_succeeds(self):
        result = self.ok("start", "bn1", "-p", "t", "--batch", "no-title", "STATUS: PASS")
        self.assertIn("--batch-title", result.stderr)
        self.assertIn("no-title", result.stderr)
        # 有标题的批次不再提醒
        result = self.ok("start", "bn2", "-p", "t", "--batch", "titled", "--batch-title", "T",
                         "--channels", "none", "SLEEP 30")
        self.assertNotIn("--batch-title", result.stderr)
        self.ok("stop", "bn1")
        self.ok("stop", "bn2")

    def test_follows_validated_and_stored(self):
        self.assertEqual(self.cli("start", "f1", "-p", "t", "--follows", "nope", "STATUS: PASS").returncode, 2)
        self.ok("start", "f0", "-p", "t", "STATUS: PASS", env_extra=self.SUP)
        self.ok("wait", "f0", "--timeout", "20")
        self.ok("start", "f1", "-p", "t", "--follows", "f0", "STATUS: PASS")
        self.assertEqual(self.job("f1")["follows"], ["f0"])
        self.ok("wait", "f0", "--timeout", "20")
        self.ok("wait", "f1", "--timeout", "20")

    def test_batches_and_batch_commands(self):
        self.ok("start", "bb1", "-p", "t", "--batch", "bb", "--batch-title", "出库回归", "STATUS: PASS")
        self.ok("start", "bb2", "-p", "t", "--batch", "bb", "--channels", "none", "SLEEP 30")
        self.ok("wait", "bb1", "--timeout", "20")
        listing = self.ok("batches").stdout
        self.assertIn("出库回归", listing)
        self.assertIn("bb", listing)
        # 不存在的批次报错
        self.assertEqual(self.cli("batch", "bb2x", "--title", "x").returncode, 2)
        self.ok("batch", "bb", "--reason", "回归出库场景")
        data = self.read_batch_file("bb")
        self.assertEqual((data["title"], data["reason"]), ("出库回归", "回归出库场景"))
        detail = self.ok("batch", "bb").stdout
        self.assertIn("出库回归", detail)
        self.assertIn("bb1", detail)
        self.ok("stop", "bb2")

    def test_api_batches_and_post(self):
        import socket, urllib.error, urllib.request
        self.ok("start", "ab1", "-p", "t", "--batch", "ab", "--batch-title", "接口事项",
                "SLEEP 30", env_extra=self.SUP)
        self.ok("start", "ab2", "-p", "t", "--batch", "ab", "--follows", "ab1", "--channels", "none",
                "SLEEP 30", env_extra=self.SUP)
        # 历史批次：没有登记文件也不报错，标题为空
        os.makedirs(os.path.join(self.home, "jobs", "legacy9"))
        with open(os.path.join(self.home, "jobs", "legacy9", "job.json"), "w") as f:
            json.dump({"name": "legacy9", "profile": "t", "state": "done", "batch": "old-batch",
                       "cwd": self.work, "turn": 1, "created": "2024-01-01 00:00:00"}, f)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = str(sock.getsockname()[1])

        def call(method, path, body=None, header=True):
            request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method,
                                             data=json.dumps(body or {}).encode() if body else None)
            request.add_header("Content-Type", "application/json")
            if header:
                request.add_header("X-Agent-Dispatch", "1")
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    return response.status, json.load(response)
            except urllib.error.HTTPError as exc:
                try:
                    return exc.code, json.load(exc)
                finally:
                    exc.close()

        try:
            self.ok("web", "start", "--port", port)
            status, data = call("GET", "/api/batches", header=False)
            self.assertEqual(status, 200)
            rows = {r["id"]: r for r in data["batches"]}
            self.assertIn("ab", rows)
            self.assertIn("old-batch", rows)  # 历史批次不报错
            self.assertEqual(rows["old-batch"]["title"], "")
            row = rows["ab"]
            self.assertEqual(row["title"], "接口事项")
            self.assertEqual(row["dispatcher"]["kind"], "pi")
            self.assertEqual(row["counts"]["running"], 2)
            self.assertEqual(row["counts"]["review"], 0)
            self.assertEqual(row["jobs"], ["ab1", "ab2"])
            # 总览作业带 batch_title / follows / dispatcher
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/overview", timeout=5) as response:
                overview = json.load(response)
            job = next(j for j in overview["jobs"] if j["name"] == "ab2")
            self.assertEqual(job["batch_title"], "接口事项")
            self.assertEqual(job["follows"], ["ab1"])
            self.assertEqual(job["dispatcher"]["session_id"], "s-batch")
            # 改标题要走写校验
            self.assertEqual(call("POST", "/api/batches/ab", {"title": "改后"}, header=False)[0], 403)
            status, _ = call("POST", "/api/batches/ab", {"title": "改后", "reason": "r"})
            self.assertEqual(status, 200)
            self.assertEqual(self.read_batch_file("ab")["title"], "改后")
        finally:
            self.ok("web", "stop")
            self.ok("stop", "ab1")
            self.ok("stop", "ab2")

    def test_web_dispatch_accepts_batch_meta(self):
        import socket, urllib.request
        self.make_project()
        port = self.start_web()
        try:
            request = urllib.request.Request(f"http://127.0.0.1:{port}/api/jobs", method="POST",
                                             data=json.dumps({"project": "p1", "role": "dev", "task": "STATUS: PASS",
                                                              "batch": "wb1", "batch_title": "网页事项",
                                                              "batch_reason": "从网页派", "follows": []}).encode())
            request.add_header("Content-Type", "application/json")
            request.add_header("X-Agent-Dispatch", "1")
            with urllib.request.urlopen(request, timeout=70) as response:
                data = json.load(response)
            self.assertTrue(data["ok"], data)
            self.assertEqual(self.read_batch_file("wb1")["title"], "网页事项")
        finally:
            self.ok("web", "stop")


class DispatcherSessionTest(DispatchBase):
    """派工者登记：识别、会话文件、sessions 查询、网页接口。"""

    def test_identify_dispatcher_four_cases(self):
        sys.path.insert(0, ROOT)
        from agent_dispatch import store
        # 1. 执行者嵌套派工：取父作业的会话标识
        who = store.identify_dispatcher({"AGENT_DISPATCH_WORKER": "j-x"}, 1, lambda n: {"session_id": "ses-1"})
        self.assertEqual((who["kind"], who["session_id"], who["parent_job"]), ("执行者", "ses-1", "j-x"))
        # 2. 主管自设会话
        who = store.identify_dispatcher({"AGENT_DISPATCH_SESSION": "s-sup", "AGENT_DISPATCH_SESSION_KIND": "网页"}, 1)
        self.assertEqual((who["kind"], who["session_id"]), ("网页", "s-sup"))
        who = store.identify_dispatcher({"AGENT_DISPATCH_SESSION": "s-sup"}, 1)
        self.assertEqual(who["kind"], "主管")  # 类型缺省为主管
        # 3. Claude Code 会话
        who = store.identify_dispatcher({"CLAUDE_CODE_SESSION_ID": "cc-9"}, 1)
        self.assertEqual((who["kind"], who["session_id"]), ("Claude Code", "cc-9"))
        # 4. 终端：TMUX_PANE 带服务器标识、TERM_SESSION_ID、ppid 兜底
        who = store.identify_dispatcher({"TMUX_PANE": "%3", "TMUX": "/tmp/tmux-501/default,123,0"}, 1)
        self.assertEqual((who["kind"], who["session_id"]), ("终端", "tmux:default:%3"))
        who = store.identify_dispatcher({"TERM_SESSION_ID": "w0t0p1"}, 1)
        self.assertEqual((who["kind"], who["session_id"]), ("终端", "w0t0p1"))
        who = store.identify_dispatcher({}, 4242)
        self.assertEqual((who["kind"], who["session_id"]), ("终端", "ppid-4242"))

    SUP = {"AGENT_DISPATCH_SESSION": "s-aaa", "AGENT_DISPATCH_SESSION_KIND": "pi"}

    def test_same_session_two_jobs_and_counts(self):
        self.ok("start", "d1", "-p", "t", "--batch", "b1", "STATUS: PASS", env_extra=self.SUP)
        self.ok("start", "d2", "-p", "t", "--batch", "b1", "--channels", "none", "SLEEP 30", env_extra=self.SUP)
        self.ok("start", "d3", "-p", "t", "--batch", "b1", "--channels", "none", "SLEEP 30", env_extra=self.SUP)
        for job in ("d1", "d2", "d3"):
            self.assertEqual(self.job(job)["dispatcher"]["session_id"], "s-aaa")
        self.ok("wait", "d1", "--timeout", "20")
        self.ok("stop", "d2", "--reason", "测试", env_extra=self.SUP)
        listing = self.ok("sessions").stdout
        self.assertIn("pi", listing)
        self.assertIn("b1", listing)  # 无标题时退回首个批次名
        self.assertRegex(listing, r"\b3\s+1\s+1\s+1\s+0\b")  # 总数 3：执行中/待审查/已完成各 1
        with open(os.path.join(self.home, "sessions", "s-aaa.json")) as f:
            session = json.load(f)
        self.assertEqual(session["id"], "s-aaa")
        self.assertIn("last_seen", session)

    def test_session_title_persists(self):
        self.ok("start", "t1", "-p", "t", "--session-title", "修复登录问题", "STATUS: PASS",
                env_extra=self.SUP)
        self.assertEqual(self.job("t1")["dispatcher"]["title"], "修复登录问题")
        # 第二次不带标题：不清空已登记的标题
        self.ok("start", "t2", "-p", "t", "STATUS: PASS", env_extra=self.SUP)
        self.assertIsNone(self.job("t2")["dispatcher"]["title"])
        with open(os.path.join(self.home, "sessions", "s-aaa.json")) as f:
            self.assertEqual(json.load(f)["title"], "修复登录问题")
        self.assertIn("修复登录问题", self.ok("sessions").stdout)

    def test_legacy_jobs_unregistered(self):
        os.makedirs(os.path.join(self.home, "jobs", "old1"))
        with open(os.path.join(self.home, "jobs", "old1", "job.json"), "w") as f:
            json.dump({"name": "old1", "profile": "t", "state": "done", "cwd": self.work, "turn": 1,
                       "created": "2024-01-01 00:00:00"}, f)
        listing = self.ok("sessions").stdout
        self.assertIn("未登记", listing)
        self.assertRegex(listing, r"\b1\s+0\s+1\s+0\s+0\b")  # 一张历史作业，归到未登记

    def test_decisions_have_by(self):
        self.ok("start", "b1", "-p", "t", "MKRUN rb\nSTATUS: PASS\nRUN_DIR: runs/rb", env_extra=self.SUP)
        self.ok("wait", "b1", "--timeout", "20")
        self.ok("gate", "b1")
        self.ok("accept", "b1", env_extra=self.SUP)
        decisions = self.job("b1")["decisions"]
        self.assertTrue(decisions)
        for d in decisions:
            if d["action"] == "accept":
                self.assertEqual((d["by"]["kind"], d["by"]["session_id"]), ("pi", "s-aaa"))

    def test_api_sessions(self):
        import socket, urllib.request
        self.ok("start", "a1", "-p", "t", "--session-title", "总览按会话看", "STATUS: PASS",
                env_extra=self.SUP)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = str(sock.getsockname()[1])
        try:
            self.ok("web", "start", "--port", port)
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/sessions", timeout=5) as response:
                data = json.load(response)
            self.assertIn("sessions", data)
            row = next(r for r in data["sessions"] if r["id"] == "s-aaa")
            self.assertEqual(row["kind"], "pi")
            self.assertEqual(row["title"], "总览按会话看")
            self.assertEqual(row["total"], 1)
            self.assertIn("a1", row["jobs"])
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/overview", timeout=5) as response:
                overview = json.load(response)
            job = next(j for j in overview["jobs"] if j["name"] == "a1")
            self.assertEqual(job["dispatcher"]["session_id"], "s-aaa")
        finally:
            self.ok("web", "stop")


class OpencodeReaderTest(unittest.TestCase):
    """会话读取：大输出要读全；读不成要返回 None 而不是「空会话」。"""

    def read(self, script):
        sys.path.insert(0, ROOT)
        from agent_dispatch import backends
        backend = {"session_reader": {"argv": [sys.executable, "-c", script, "{session}"]}}
        return backends.read_session_opencode(backend, {"session_id": "ses_x", "cwd": os.getcwd()})

    def test_large_export_is_read_completely(self):
        script = (
            "import json\n"
            "big = 'x' * 400000\n"
            "print(json.dumps({'messages': ["
            "{'type': 'user', 'text': 'hi'},"
            "{'type': 'assistant', 'time': {'completed': 2}, 'content': ["
            "{'type': 'tool', 'name': 'shell', 'state': {'status': 'completed', 'input': {}, 'content': [{'type': 'text', 'text': big}]}, 'time': {'ran': 1, 'completed': 2}},"
            "{'type': 'text', 'text': 'STATUS: PASS'}]},"
            "{'type': 'idle', 'outcome': 'succeeded'}]}))"
        )
        items, idle = self.read(script)
        self.assertTrue(idle)
        self.assertEqual([item["kind"] for item in items], ["user", "tool", "text"])
        self.assertEqual(len(items[1]["output"]), 400000)

    def test_broken_export_is_not_an_empty_session(self):
        self.assertIsNone(self.read("print('{\"messages\": [{\"type\": \"user\"')"))

    def test_missing_session_is_empty(self):
        self.assertEqual(self.read("import sys; sys.exit(1)"), ([], False))

    def test_provider_error_is_readable(self):
        script = ("import json\n"
                  "print(json.dumps({'messages': [{'type': 'user', 'text': 'go'},"
                  "{'type': 'assistant', 'time': {}, 'error': {'type': 'provider.quota', 'message': 'quota exceeded'}, 'content': []},"
                  "{'type': 'idle', 'outcome': 'failed'}]}))")
        items, idle = self.read(script)
        self.assertTrue(idle)
        self.assertEqual([item.get("message") for item in items if item["kind"] == "error"],
                         ["quota exceeded", "回合结束，结果：failed"])

    def test_unfinished_assistant_message_is_not_idle(self):
        script = ("import json\n"
                  "print(json.dumps({'messages': [{'type': 'idle'}, {'type': 'user', 'text': 'go'},"
                  "{'type': 'assistant', 'time': {}, 'content': [{'type': 'text', 'text': 'working'}]}]}))")
        items, idle = self.read(script)
        self.assertFalse(idle)
        self.assertEqual([item["kind"] for item in items], ["user"])


class RunCliStdoutTest(unittest.TestCase):
    """run_cli 要把标准输出单独交回：预览解析 JSON 不能被 stderr 里的提示污染。"""

    def test_stdout_is_returned_separately(self):
        sys.path.insert(0, ROOT)
        import agent_dispatch.web as web
        payload = json.dumps({"brief": "你是测试工程师", "job": "j9"}, ensure_ascii=False)

        class Proc:
            returncode = 0
            stdout = payload
            stderr = "[agent-dispatch] 提示：stderr 里有些无关输出\n"

        original = web.subprocess.run
        web.subprocess.run = lambda *a, **k: Proc()
        try:
            code, text, error, out = web.run_cli(["start", "j9", "--dry-run", "--json", "-"], "跑一下")
        finally:
            web.subprocess.run = original
        self.assertEqual(code, 0)
        data = json.loads(out)  # 只解析标准输出，stderr 的提示不混进来
        self.assertEqual(data["job"], "j9")
        self.assertIn("无关输出", text)  # 提示文字仍然能看到


class RegistryFixtureBase(DispatchBase):
    """MCP 与技能登记测试的公共环境：假的用户主目录 + 项目工程来源。
    只含搭建与辅助方法，不含测试用例，供多个测试类继承。
    导入来源全在临时目录，不读真实的 ~/.pi 等配置。"""

    def setUp(self):
        super().setUp()
        # 假的用户主目录：只作为导入来源，测试绝不读真实的 ~/.pi、~/.config/opencode、~/.claude
        self.fakehome = os.path.join(self.tmp, "userhome")
        pi_mcp = {"mcpServers": {
            "pi-server": {"command": "npx", "args": ["-y", "pkg", "--password", "s3cret"],
                          "env": {"TOKEN": "tok-123", "NODE_ENV": "production"}},
            "shared": {"command": "pi-cmd"},
            "dupe": {"command": "same-cmd"}}}
        os.makedirs(os.path.join(self.fakehome, ".pi", "agent"))
        with open(os.path.join(self.fakehome, ".pi", "agent", "mcp.json"), "w") as f:
            json.dump(pi_mcp, f)
        oc_cfg = {"mcp": {
            "oc-server": {"type": "local", "command": ["oc-run", "x"]},
            "shared": {"type": "local", "command": ["oc-run"]},
            "off-one": {"type": "local", "command": ["nope"], "enabled": False}}}
        os.makedirs(os.path.join(self.fakehome, ".config", "opencode"))
        with open(os.path.join(self.fakehome, ".config", "opencode", "opencode.json"), "w") as f:
            json.dump(oc_cfg, f)
        for rel, desc in (((".claude", "skills", "demo-skill"), "演示技能的说明"),
                          ((".pi", "agent", "skills", "pi-skill"), None),
                          ((".claude", "skills", "dual"), "甲版本"),
                          ((".agents", "skills", "dual"), "乙版本")):
            directory = os.path.join(self.fakehome, *rel)
            os.makedirs(directory)
            text = "# 没有前置说明的技能\n正文\n" if desc is None else f"---\ndescription: {desc}\n---\n# 标题\n"
            with open(os.path.join(directory, "SKILL.md"), "w") as f:
                f.write(text)
        # 同内容拷贝（demo-skill 的第二份）与软链接（pi-skill 的别名）：都应合并成一个变体
        copy_dir = os.path.join(self.fakehome, ".agents", "skills", "demo-skill")
        os.makedirs(copy_dir)
        with open(os.path.join(copy_dir, "SKILL.md"), "w") as f:
            f.write("---\ndescription: 演示技能的说明\n---\n# 标题\n")
        os.makedirs(os.path.join(self.fakehome, ".cc-switch", "skills"))
        os.symlink(os.path.join(self.fakehome, ".pi", "agent", "skills", "pi-skill"),
                   os.path.join(self.fakehome, ".cc-switch", "skills", "pi-skill"))
        os.makedirs(os.path.join(self.fakehome, ".claude", "skills", "noskill"))  # 没有 SKILL.md，不算技能
        with open(os.path.join(self.fakehome, ".claude", "skills", "noskill", "README.md"), "w") as f:
            f.write("x")
        # 项目工程里的来源（make_project 的 app 工程就是 self.work）
        os.makedirs(os.path.join(self.work, ".pi"))
        with open(os.path.join(self.work, ".pi", "mcp.json"), "w") as f:
            json.dump({"mcpServers": {"proj-mcp": {"command": "proj-cmd", "args": ["--dsn", "mysql://u:pw@h/db"]},
                                      "dupe": {"command": "same-cmd"}}}, f)  # dupe 与 pi 全局内容相同：合并
        with open(os.path.join(self.work, "opencode.json"), "w") as f:
            json.dump({"mcp": {"proj-oc": {"type": "remote", "url": "https://x/sse",
                                          "headers": {"Authorization": "Bearer t"}}}}, f)
        proj_skill = os.path.join(self.work, ".agents", "skills", "proj-skill")
        os.makedirs(proj_skill)
        with open(os.path.join(proj_skill, "SKILL.md"), "w") as f:
            f.write("---\ndescription: 项目内技能\n---\n")
        self.import_env = {"AGENT_DISPATCH_IMPORT_HOME": self.fakehome}
        self.make_project()  # 项目工程里的 .pi/mcp.json、opencode.json、技能目录也是导入来源

    def make_project(self):
        # 幂等：setUp 里建过一次后，继承来的老测试两次要建时直接跳过
        if getattr(self, "_project_made", False):
            return
        self._project_made = True
        super().make_project()

    def _snapshot_sources(self):
        paths = [self.fakehome, os.path.join(self.work, ".pi"), os.path.join(self.work, ".agents"),
                 os.path.join(self.work, "opencode.json")]
        out = {}
        for base in paths:
            if os.path.isfile(base):
                with open(base, "rb") as f:
                    out[base] = f.read()
            elif os.path.isdir(base):
                for root, _, names in os.walk(base):
                    for name in names:
                        with open(os.path.join(root, name), "rb") as f:
                            out[os.path.join(root, name)] = f.read()
        return out

    def py(self, statements):
        import textwrap
        script = ("import json, os, sys\nsys.path.insert(0, %r)\n"
                  "from agent_dispatch import definitions, registry\n"
                  "%s\nprint('@@' + json.dumps(globals().get('result'), ensure_ascii=False, default=str))") % (
            ROOT, textwrap.dedent(statements))
        env = dict(os.environ, AGENT_DISPATCH_HOME=self.home, AGENT_DISPATCH_BACKENDS=self.backends,
                   AGENT_DISPATCH_IMPORT_HOME=self.fakehome, PYTHONPATH=ROOT)
        for key in ("AGENT_DISPATCH_WORKER", "AGENT_DISPATCH_SESSION", "TMUX_PANE"):
            env.pop(key, None)
        proc = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads([l for l in proc.stdout.splitlines() if l.startswith("@@")][-1][2:])

    def raises(self, statements):
        import textwrap
        script = ("import json, sys\nsys.path.insert(0, %r)\n"
                  "from agent_dispatch import definitions, registry\nfrom agent_dispatch.store import DispatchError\n"
                  "try:\n%s\n"
                  "except DispatchError as e:\n    print('@@' + str(e))\nelse:\n    raise SystemExit('没有抛 DispatchError')") % (
            ROOT, textwrap.indent(textwrap.dedent(statements), "    "))
        env = dict(os.environ, AGENT_DISPATCH_HOME=self.home, AGENT_DISPATCH_BACKENDS=self.backends,
                   AGENT_DISPATCH_IMPORT_HOME=self.fakehome, PYTHONPATH=ROOT)
        proc = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        return [l for l in proc.stdout.splitlines() if l.startswith("@@")][-1][2:]

    def group(self, candidates, name):
        return next(g for g in candidates if g["name"] == name)

class McpSkillRegistryTest(RegistryFixtureBase):
    """MCP 与技能的登记（登记 ID 唯一、name 下发）、导入合并、遮罩和按角色配置。"""

    def test_mcp_candidates_grouped_with_diff(self):
        out = self.py("result = registry.mcp_candidates()")
        shared = self.group(out, "shared")
        self.assertTrue(shared["conflict"])
        self.assertEqual({v["id"] for v in shared["variants"]}, {"shared@pi-global", "shared@oc-global"})
        self.assertTrue(all(v["name"] == "shared" for v in shared["variants"]))  # name 保持服务名
        self.assertIn("command", shared["variants"][0]["diff"])  # 差异摘要指出哪些参数不同
        # 内容相同的来源合并成一个变体并记下全部来源
        dupe = self.group(out, "dupe")
        self.assertFalse(dupe["conflict"])
        self.assertEqual(dupe["variants"][0]["id"], "dupe")
        self.assertEqual(len(dupe["variants"][0]["sources"]), 2)
        self.assertTrue(any("pi 全局" in s for s in dupe["variants"][0]["sources"]))
        self.assertTrue(any("项目" in s for s in dupe["variants"][0]["sources"]))
        pi = self.group(out, "pi-server")["variants"][0]
        self.assertEqual((pi["entry"]["type"], pi["entry"]["command"]), ("local", "npx"))
        self.assertEqual(pi["entry"]["env"]["NODE_ENV"], "production")
        oc = self.group(out, "oc-server")["variants"][0]
        self.assertEqual((oc["entry"]["command"], oc["entry"]["args"]), ("oc-run", ["x"]))  # OpenCode 数组归一
        proj = self.group(out, "proj-oc")["variants"][0]
        self.assertEqual((proj["entry"]["type"], proj["entry"]["url"]), ("remote", "https://x/sse"))
        self.assertIn("项目", self.group(out, "proj-mcp")["variants"][0]["source"])
        self.assertFalse([g for g in out if g["name"] == "off-one"])  # enabled:false 的不带进来

    def test_mcp_import_variants_and_merge(self):
        err = self.raises("registry.import_mcp(['shared'])")
        self.assertIn("shared@pi-global", err)
        self.assertIn("shared@oc-global", err)
        out = self.py("result = registry.import_mcp(['shared@oc-global'])")  # 按登记 ID 选
        self.assertEqual(out, ["shared@oc-global"])
        out = self.py("""
            result = registry.import_mcp([
                {'name': 'shared', 'source': 'pi 全局 ~/.pi/agent/mcp.json'},
                'dupe', 'pi-server', 'oc-server', 'proj-mcp'])
        """)
        self.assertEqual(sorted(out), sorted(["shared@pi-global", "dupe", "pi-server", "oc-server", "proj-mcp"]))
        data = registry_load(self, "mcp")
        # 同名不同内容各成一条，name 都是原名
        self.assertEqual(data["shared@oc-global"]["name"], "shared")
        self.assertEqual(data["shared@oc-global"]["command"], "oc-run")
        self.assertEqual(data["shared@pi-global"]["name"], "shared")
        self.assertEqual(data["shared@pi-global"]["command"], "pi-cmd")
        self.assertEqual(len(data["dupe"]["sources"]), 2)  # 相同内容合并，记下全部来源
        self.assertEqual(data["pi-server"]["env"]["TOKEN"], "tok-123")  # 磁盘上保留原值
        self.assertIn("s3cret", data["pi-server"]["args"])
        again = self.py("result = registry.import_mcp(['pi-server', 'dupe'])")
        self.assertEqual(again, [])  # 已登记且相同：跳过

    def test_masking_only_secret_env_and_placeholder(self):
        self.py("registry.import_mcp(['pi-server', 'proj-mcp'])")
        out = self.py("result = {i: registry.mask_entry(e) for i, e in registry.load_mcp().items()}")
        text = json.dumps(out, ensure_ascii=False)
        self.assertNotIn("tok-123", text)          # TOKEN 键名含敏感字样：遮
        self.assertNotIn("s3cret", text)           # --password 后面的值：遮
        self.assertNotIn("mysql://u:pw@h/db", text)  # 带凭据的 URL：遮
        self.assertIn("production", text)          # NODE_ENV 这类不是秘密：不遮
        self.assertIn("••••", text)
        listing = self.ok("mcp", "list", env_extra=self.import_env).stdout
        self.assertNotIn("s3cret", listing)
        self.assertIn("••••", listing)
        # 提交遮罩占位不覆盖原值
        self.py("""
            registry.save_mcp('pi-server', {'type': 'local', 'command': 'npx',
                'args': ['-y', 'pkg', '--password', registry.MASK], 'env': {'TOKEN': registry.MASK, 'NODE_ENV': 'production'}})
            result = registry.load_mcp()['pi-server']
        """)
        data = registry_load(self, "mcp")["pi-server"]
        self.assertEqual(data["env"]["TOKEN"], "tok-123")
        self.assertEqual(data["env"]["NODE_ENV"], "production")
        self.assertIn("s3cret", data["args"])

    def test_skills_dedupe_by_path_and_content(self):
        out = self.py("result = registry.skill_candidates()")
        names = {g["name"] for g in out}
        self.assertEqual(names, {"demo-skill", "pi-skill", "proj-skill", "dual"})  # 没有 SKILL.md 的不算
        demo = self.group(out, "demo-skill")
        self.assertFalse(demo["conflict"])  # 同内容拷贝合并成一个变体
        self.assertEqual(len(demo["variants"]), 1)
        self.assertEqual(len(demo["variants"][0]["sources"]), 2)
        self.assertEqual(demo["variants"][0]["description"], "演示技能的说明")
        pi_skill = self.group(out, "pi-skill")
        self.assertEqual(len(pi_skill["variants"]), 1)  # 软链接按真实路径去重
        dual = self.group(out, "dual")
        self.assertTrue(dual["conflict"])
        self.assertEqual({v["id"] for v in dual["variants"]}, {"dual@claude", "dual@agents"})
        added = self.py("result = registry.import_skills(['demo-skill', 'pi-skill', 'proj-skill', 'dual@claude', 'dual@agents'])")
        self.assertEqual(sorted(added), ["demo-skill", "dual@agents", "dual@claude", "pi-skill", "proj-skill"])
        data = registry_load(self, "skills")
        self.assertTrue(data["proj-skill"]["path"].endswith("proj-skill"))
        self.assertEqual(data["demo-skill"]["description"], "演示技能的说明")
        self.assertEqual(data["dual@claude"]["name"], "dual")  # name 保持技能名
        self.assertEqual(data["dual@agents"]["name"], "dual")
        self.assertEqual(data["dual@claude"]["description"], "甲版本")
        self.assertEqual(data["dual@agents"]["description"], "乙版本")
        self.assertIn("dual@claude", self.ok("skills", "list", env_extra=self.import_env).stdout)

    def test_role_rejects_duplicate_service_name(self):
        self.make_project()
        self.py("registry.import_mcp(['pi-server', 'shared@pi-global', 'shared@oc-global'])\n"
                "registry.import_skills(['dual@claude', 'dual@agents'])")
        err = self.raises("""
            definitions.set_role_fields(definitions.load_project('p1'), 'qa',
                {'mcp': ['shared@pi-global', 'shared@oc-global']})
        """)
        self.assertIn("shared", err)
        self.assertIn("shared@pi-global", err)
        self.assertIn("shared@oc-global", err)
        err = self.raises("""
            definitions.set_role_fields(definitions.load_project('p1'), 'qa',
                {'skills': ['dual@claude', 'dual@agents']})
        """)
        self.assertIn("dual", err)
        err = self.raises("""
            definitions.set_role_fields(definitions.load_project('p1'), 'qa', {'mcp': ['nope'], 'skills': []})
        """)
        self.assertIn("nope", err)
        err = self.raises("""
            definitions.set_role_fields(definitions.load_project('p1'), 'qa', {'skills': 'demo-skill'})
        """)
        self.assertIn("skills", err)
        out = self.py("""
            project = definitions.load_project('p1')
            definitions.set_role_fields(project, 'qa', {'mcp': ['shared@oc-global', 'pi-server'], 'skills': ['dual@claude']})
            definitions.set_role_fields(project, 'dev', {'mcp': []})  # 空列表 = 由 agent-dispatch 管理，一个都不带
            result = {'qa': definitions.role_detail(project, 'qa'), 'dev': definitions.role_detail(project, 'dev')}
        """)
        self.assertEqual(out["qa"]["fields"]["mcp"]["value"], ["shared@oc-global", "pi-server"])
        self.assertEqual(out["qa"]["fields"]["mcp"]["source"], "项目")
        self.assertEqual(out["dev"]["fields"]["mcp"]["value"], [])
        show = self.ok("roles", "show", "qa", "-P", "p1", env_extra=self.import_env).stdout
        self.assertIn("shared@oc-global", show)
        self.assertIn("dual@claude", show)

    def test_remove_referenced_is_refused(self):
        self.make_project()
        self.py("""
            registry.import_mcp(['shared@pi-global'])
            registry.import_skills(['demo-skill'])
            definitions.set_role_fields(definitions.load_project('p1'), 'qa', {'mcp': ['shared@pi-global'], 'skills': ['demo-skill']})
        """)
        err = self.raises("registry.remove_mcp('shared@pi-global')")
        self.assertIn("p1", err)
        self.assertIn("qa", err)
        err = self.raises("registry.remove_skill('demo-skill')")
        self.assertIn("qa", err)
        self.assertIn("没有", self.raises("registry.remove_mcp('shared')"))  # 没有 name 这个登记 ID
        # 去掉角色的引用后就能删
        self.py("definitions.reset_role_fields(definitions.load_project('p1'), 'qa', ['mcp', 'skills'])")
        self.py("registry.remove_mcp('shared@pi-global')\nregistry.remove_skill('demo-skill')")
        self.assertNotIn("shared@pi-global", registry_load(self, "mcp"))

    def test_web_mcp_skills_api(self):
        import socket, urllib.error, urllib.request
        self.make_project()
        self.py("registry.import_mcp(['pi-server', 'oc-server', 'shared@oc-global'])\nregistry.import_skills(['demo-skill'])")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = str(sock.getsockname()[1])
        base = f"http://127.0.0.1:{port}"

        def call(method, path, body=None, header=True):
            request = urllib.request.Request(base + path, method=method, data=json.dumps(body or {}).encode())
            request.add_header("Content-Type", "application/json")
            if header:
                request.add_header("X-Agent-Dispatch", "1")
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    return response.status, json.load(response)
            except urllib.error.HTTPError as exc:
                try:
                    return exc.code, json.load(exc)
                finally:
                    exc.close()

        try:
            self.ok("web", "start", "--port", port, env_extra=self.import_env)
            # 写操作没有请求头被拒
            self.assertEqual(call("POST", "/api/mcp", {"name": "x"}, header=False)[0], 403)
            self.assertEqual(call("DELETE", "/api/mcp/x", header=False)[0], 403)
            # 登记表已遮罩，登记 ID 与 name 都可见，并带被哪些角色使用
            status, data = call("GET", "/api/mcp")
            self.assertEqual(status, 200)
            text = json.dumps(data, ensure_ascii=False)
            self.assertNotIn("tok-123", text)
            self.assertNotIn("s3cret", text)
            self.assertIn("production", text)  # 非秘密的 env 值不遮
            pi = next(m for m in data["mcp"] if m["id"] == "pi-server")
            self.assertEqual((pi["name"], pi["env"]["TOKEN"], pi["env"]["NODE_ENV"]),
                             ("pi-server", "••••", "production"))
            shared = next(m for m in data["mcp"] if m["id"] == "shared@oc-global")
            self.assertEqual(shared["name"], "shared")
            # 候选按服务名分组，同名冲突列出各变体与差异
            groups = call("GET", "/api/mcp/candidates")[1]["candidates"]
            shared_group = next(g for g in groups if g["name"] == "shared")
            self.assertTrue(shared_group["conflict"])
            self.assertEqual({v["id"] for v in shared_group["variants"]}, {"shared@pi-global", "shared@oc-global"})
            self.assertIn("command", shared_group["variants"][0]["diff"])
            # 导入：按登记 ID；同名不给来源被拒
            self.assertEqual(call("POST", "/api/mcp/import", {"names": ["proj-mcp"]})[0], 200)
            status, error = call("POST", "/api/mcp/import", {"names": ["shared"]})
            self.assertEqual(status, 400)
            self.assertIn("shared@oc-global", error["error"])
            self.assertEqual(call("POST", "/api/mcp/import",
                                  {"names": ["shared"], "selections": [{"name": "shared", "source": "pi 全局 ~/.pi/agent/mcp.json"}]})[0], 200)
            # 新增、修改（遮罩占位不覆盖）、删除
            self.assertEqual(call("POST", "/api/mcp", {"name": "mine", "entry": {
                "type": "local", "command": "run-it", "args": ["--token", "abc"], "env": {"API_KEY": "V"}}})[0], 200)
            self.assertEqual(call("POST", "/api/mcp", {"name": "mine", "entry": {
                "type": "local", "command": "run-it", "env": {"API_KEY": "••••"}}})[0], 200)
            mine = next(m for m in call("GET", "/api/mcp")[1]["mcp"] if m["id"] == "mine")
            self.assertEqual(mine["env"], {"API_KEY": "••••"})
            self.assertEqual(registry_load(self, "mcp")["mine"]["env"]["API_KEY"], "V")
            self.assertEqual(call("DELETE", "/api/mcp/mine")[0], 200)
            # 被角色引用时删除被拒
            self.py("definitions.set_role_fields(definitions.load_project('p1'), 'qa', {'mcp': ['shared@oc-global']})")
            status, error = call("DELETE", "/api/mcp/shared@oc-global")
            self.assertEqual(status, 400)
            self.assertIn("qa", error["error"])
            # 技能同样的接口
            skills = call("GET", "/api/skills")[1]["skills"]
            self.assertEqual(skills[0]["id"], "demo-skill")
            self.assertEqual(call("POST", "/api/skills/import", {"names": ["pi-skill"]})[0], 200)
            self.assertEqual(call("POST", "/api/skills", {"name": "mine-s", "path":
                os.path.join(self.fakehome, ".claude", "skills", "demo-skill")})[0], 200)
            self.assertEqual(registry_load(self, "skills")["mine-s"]["name"], "demo-skill")  # name 默认取目录名
            self.assertEqual(call("DELETE", "/api/skills/mine-s")[0], 200)
            skill_groups = call("GET", "/api/skills/candidates")[1]["candidates"]
            self.assertEqual({g["name"] for g in skill_groups}, {"demo-skill", "pi-skill", "proj-skill", "dual"})
            # 角色详情接口自动带出 mcp 字段（存登记 ID）
            detail = call("GET", "/api/config/roles/qa?project=p1")[1]
            self.assertEqual(detail["fields"]["mcp"]["value"], ["shared@oc-global"])
        finally:
            self.ok("web", "stop")

    def test_import_sources_untouched(self):
        before = self._snapshot_sources()
        self.ok("mcp", "import", "--all", env_extra=self.import_env)
        self.ok("skills", "import", "--all", env_extra=self.import_env)
        after = self._snapshot_sources()
        self.assertEqual(before, after)

    def test_cli_mcp_add_and_skills_add(self):
        self.ok("mcp", "add", "manual", "--command", "run-x", "--args", "a,b", "--env", "K=V",
                "--description", "手动的", env_extra=self.import_env)
        self.ok("skills", "add", "manual-skill", os.path.join(self.fakehome, ".claude", "skills", "demo-skill"),
                env_extra=self.import_env)
        data = registry_load(self, "mcp")
        self.assertEqual((data["manual"]["command"], data["manual"]["args"]), ("run-x", ["a", "b"]))
        self.assertEqual(data["manual"]["description"], "手动的")
        self.assertEqual(data["manual"]["name"], "manual")  # name 缺省等于登记 ID
        self.assertEqual(registry_load(self, "skills")["manual-skill"]["description"], "演示技能的说明")
        self.assertEqual(registry_load(self, "skills")["manual-skill"]["name"], "demo-skill")  # name 默认取目录名
        # 既给 command 又给 url 的写法不对
        result = self.cli("mcp", "add", "bad", "--command", "x", "--url", "https://x", env_extra=self.import_env)
        self.assertEqual(result.returncode, 2)


def registry_load(test, kind):
    """直接读临时状态目录里的登记表文件。"""
    path = os.path.join(test.home, f"{kind}.json")
    with open(path) as f:
        return json.load(f)


class ProjectScopedRegistryTest(RegistryFixtureBase):
    """登记条目按项目归属：项目里只能看到本项目的 + 全局的，defaults 是项目级启用清单。"""

    def setUp(self):
        super().setUp()
        # 第二个项目 p2，自己的工程里有一个 p2 专属的 MCP 服务与技能
        self.p2work = os.path.join(self.tmp, "p2-work")
        os.makedirs(os.path.join(self.p2work, ".pi"))
        with open(os.path.join(self.p2work, ".pi", "mcp.json"), "w") as f:
            json.dump({"mcpServers": {"p2-only": {"command": "p2-cmd"}}}, f)
        p2skill = os.path.join(self.p2work, ".agents", "skills", "p2-skill")
        os.makedirs(p2skill)
        with open(os.path.join(p2skill, "SKILL.md"), "w") as f:
            f.write("---\ndescription: p2 的技能\n---\n")
        self.ok("projects", "add", "p2", "--repo", f"only={self.p2work}", env_extra=self.import_env)

    def test_import_attribution_and_old_data_is_global(self):
        # 按项目导入：工程里扫出来的归属该项目，全局来源的归全局
        added = self.py("result = registry.import_mcp(['proj-mcp', 'pi-server'], project='p1')")
        self.assertEqual(sorted(added), ["pi-server", "proj-mcp"])
        data = registry_load(self, "mcp")
        self.assertEqual(data["proj-mcp"]["project"], "p1")
        self.assertEqual(data["pi-server"].get("project", ""), "")
        # 旧数据没有 project 字段：视为全局，两个项目里都可用
        self.assertEqual(set(self.py("result = registry.entries_for(registry.load_mcp(), 'p1')")),
                         {"pi-server", "proj-mcp"})
        self.assertEqual(set(self.py("result = registry.entries_for(registry.load_mcp(), 'p2')")), {"pi-server"})
        # 项目候选只含该项目的工程来源 + 全局来源
        out = self.py("result = registry.mcp_candidates(project='p1')")
        names = {g["name"] for g in out}
        self.assertIn("proj-mcp", names)
        self.assertIn("pi-server", names)
        self.assertNotIn("p2-only", names)
        proj = self.group(out, "proj-mcp")["variants"][0]
        self.assertEqual(proj["project"], "p1")
        out2 = self.py("result = registry.mcp_candidates(project='p2')")
        self.assertIn("p2-only", {g["name"] for g in out2})
        self.assertNotIn("proj-mcp", {g["name"] for g in out2})
        # 全局视图（project=''）只有全局来源
        outg = self.py("result = registry.mcp_candidates(project='')")
        self.assertNotIn("proj-mcp", {g["name"] for g in outg})
        # 技能同样一套
        added = self.py("result = registry.import_skills(['proj-skill'], project='p1')")
        self.assertEqual(added, ["proj-skill"])
        self.assertEqual(registry_load(self, "skills")["proj-skill"]["project"], "p1")
        outs = self.py("result = registry.skill_candidates(project='p2')")
        self.assertIn("p2-skill", {g["name"] for g in outs})
        self.assertNotIn("proj-skill", {g["name"] for g in outs})

    def test_role_cannot_use_other_projects_entry(self):
        self.py("registry.import_mcp(['p2-only'], project='p2')\nregistry.import_skills(['p2-skill'], project='p2')")
        # p1 的角色选 p2 的条目：拒
        err = self.raises("""
            definitions.set_role_fields(definitions.load_project('p1'), 'qa', {'mcp': ['p2-only']})
        """)
        self.assertIn("p2", err)
        self.assertIn("p2-only", err)
        # 全局角色也不能选项目的条目
        err = self.raises("definitions.set_role_fields(None, 'qa', {'skills': ['p2-skill']})")
        self.assertIn("p2-skill", err)
        # p2 自己的角色可以
        self.py("definitions.set_role_fields(definitions.load_project('p2'), 'qa', {'mcp': ['p2-only']})")

    def test_project_defaults_enable_and_role_override(self):
        self.py("""
            registry.import_mcp(['pi-server', 'proj-mcp'], project='p1')
            definitions.update_project('p1', defaults={'mcp': ['pi-server']})
        """)
        profile = self.py("result = definitions.resolve(definitions.load_project('p1'), 'dev')")
        self.assertEqual(profile["mcp"], ["pi-server"])  # 项目 defaults 对所有角色生效
        self.assertEqual(self.py("result = definitions.resolve(definitions.load_project('p1'), 'qa', 'app')")["mcp"],
                         ["pi-server"])
        # 角色层单独覆盖优先
        self.py("definitions.set_role_fields(definitions.load_project('p1'), 'qa', {'mcp': ['proj-mcp']})")
        self.assertEqual(self.py("result = definitions.resolve(definitions.load_project('p1'), 'qa', 'app')")["mcp"], ["proj-mcp"])
        # 派工预览的「本次下发」按解析后的字段工作
        out = json.loads(self.ok("start", "j-d", "--project", "p1", "--role", "qa", "--repo", "app",
                                 "--dry-run", "--json", "-", stdin="做事", env_extra=self.import_env).stdout)
        self.assertTrue(out["inject"]["managed"])
        self.assertEqual([s["id"] for s in out["inject"]["mcp"]["services"]], ["proj-mcp"])
        # 同名冲突在 defaults 层也要拦
        self.py("registry.import_mcp(['shared@pi-global', 'shared@oc-global'])")
        err = self.raises("""
            definitions.update_project('p1', defaults={'mcp': ['shared@pi-global', 'shared@oc-global']})
        """)
        self.assertIn("shared", err)
        # defaults 里选别的项目的条目也要拦
        err = self.raises("registry.import_mcp(['p2-only'], project='p2');\n"
                          "definitions.update_project('p1', defaults={'mcp': ['p2-only']})")
        self.assertIn("p2", err)

    def test_defaults_none_removes_field(self):
        self.py("registry.import_mcp(['pi-server'])\n"
                "definitions.update_project('p1', defaults={'mcp': ['pi-server']})")
        self.assertIn("mcp", self.py("result = definitions.load_project('p1').get('defaults')"))
        self.py("definitions.update_project('p1', defaults={'mcp': None})")
        self.assertNotIn("mcp", self.py("result = definitions.load_project('p1').get('defaults') or {}") or {})

    def test_remove_referenced_by_defaults(self):
        self.py("registry.import_mcp(['pi-server'], project='p1')\n"
                "definitions.update_project('p1', defaults={'mcp': ['pi-server']})")
        err = self.raises("registry.remove_mcp('pi-server')")
        self.assertIn("p1", err)
        self.assertIn("项目默认", err)
        self.py("definitions.update_project('p1', defaults={'mcp': None})")
        self.py("registry.remove_mcp('pi-server')")

    def test_cli_project_flags(self):
        self.py("registry.import_mcp(['proj-mcp'], project='p1')\nregistry.import_skills(['proj-skill'], project='p1')")
        out = self.ok("mcp", "--project", "p1", "list", env_extra=self.import_env).stdout
        self.assertIn("proj-mcp", out)
        self.assertIn("本项目", out)
        self.assertNotIn("p2-only", out)
        # enable / disable 调整本项目启用清单
        self.ok("mcp", "enable", "proj-mcp", "--project", "p1", env_extra=self.import_env)
        self.assertEqual(self.py("result = definitions.load_project('p1').get('defaults')")["mcp"], ["proj-mcp"])
        self.ok("mcp", "disable", "proj-mcp", "--project", "p1", env_extra=self.import_env)
        self.assertEqual(self.py("result = definitions.load_project('p1').get('defaults')")["mcp"], [])
        # 别的项目的条目不能 enable
        result = self.cli("mcp", "enable", "p2-only", "--project", "p1", env_extra=self.import_env)
        self.assertEqual(result.returncode, 2)
        # 技能同理；import --project 归属项目
        self.ok("skills", "enable", "proj-skill", "--project", "p1", env_extra=self.import_env)
        self.assertEqual(self.py("result = definitions.load_project('p1').get('defaults')")["skills"], ["proj-skill"])
        self.ok("skills", "import", "--all", "--project", "p2", env_extra=self.import_env)
        self.assertEqual(registry_load(self, "skills")["p2-skill"]["project"], "p2")
        # roles show 标明 mcp / skills 的来源层
        show = self.ok("roles", "show", "qa", "-P", "p1", env_extra=self.import_env).stdout
        self.assertIn("项目默认", show)

    def test_web_project_scoped_api(self):
        import socket, urllib.error, urllib.request
        self.py("registry.import_mcp(['proj-mcp', 'pi-server'], project='p1')\n"
                "registry.import_skills(['proj-skill'], project='p1')")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = str(sock.getsockname()[1])
        base = f"http://127.0.0.1:{port}"

        def call(method, path, body=None):
            request = urllib.request.Request(base + path, method=method, data=json.dumps(body or {}).encode())
            request.add_header("Content-Type", "application/json")
            request.add_header("X-Agent-Dispatch", "1")
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    return response.status, json.load(response)
            except urllib.error.HTTPError as exc:
                try:
                    return exc.code, json.load(exc)
                finally:
                    exc.close()

        try:
            self.ok("web", "start", "--port", port, env_extra=self.import_env)
            # 项目视图：本项目的 + 全局的，各自标归属；未管理、启用清单、角色覆盖
            status, data = call("GET", "/api/mcp?project=p1")
            self.assertEqual(status, 200)
            by_id = {m["id"]: m for m in data["mcp"]}
            self.assertEqual(set(by_id), {"pi-server", "proj-mcp"})
            self.assertEqual(by_id["proj-mcp"]["project"], "p1")
            self.assertEqual(by_id["pi-server"]["project"], "")
            self.assertFalse(data["managed"])
            self.assertEqual(data["enabled"], [])
            self.assertTrue(any(r["id"] == "qa" for r in data["roles"]))
            # 启用清单走项目 defaults
            self.assertEqual(call("POST", "/api/config/projects/p1", {"defaults": {"mcp": ["pi-server"]}})[0], 200)
            status, data = call("GET", "/api/mcp?project=p1")
            self.assertTrue(data["managed"])
            self.assertEqual(data["enabled"], ["pi-server"])
            # 删掉字段恢复未管理
            self.assertEqual(call("POST", "/api/config/projects/p1", {"defaults": {"mcp": None}})[0], 200)
            self.assertFalse(call("GET", "/api/mcp?project=p1")[1]["managed"])
            # 候选带项目过滤
            groups = call("GET", "/api/mcp/candidates?project=p1")[1]["candidates"]
            self.assertIn("proj-oc", {g["name"] for g in groups})
            self.assertNotIn("p2-only", {g["name"] for g in groups})
            # 导入带 project：归属该项目
            self.assertEqual(call("POST", "/api/mcp/import", {"names": ["proj-oc"], "project": "p1"})[0], 200)
            self.assertEqual(registry_load(self, "mcp")["proj-oc"]["project"], "p1")
            # 未定义的项目：明确报错
            status, error = call("GET", "/api/mcp?project=nope")
            self.assertEqual(status, 404)
            self.assertIn("nope", error["error"])
            # 全局视图（不带 project）：只看全局登记，不含 p1 的条目
            data = call("GET", "/api/mcp")[1]
            self.assertEqual({m["id"] for m in data["mcp"]}, {"pi-server"})
            # 技能同样一套
            status, data = call("GET", "/api/skills?project=p1")
            self.assertEqual({s["id"] for s in data["skills"]}, {"proj-skill"})
        finally:
            self.ok("web", "stop")



class InjectTest(DispatchBase):
    """按作业下发 MCP / 技能：字段不存在 = 命令与原先一样；存在 = 只带选中的
    （pi 换作业私有 agent 目录，OpenCode 走 OPENCODE_CONFIG_CONTENT）。全用假后端，不碰真实的 ~/.pi、~/.config/opencode。"""

    def setUp(self):
        super().setUp()
        # 假的 pi agent 源目录与 OpenCode 全局配置（下发时只读它们，绝不读真实配置）
        self.pi_agent = os.path.join(self.tmp, "piagent-src")
        os.makedirs(os.path.join(self.pi_agent, "sessions"))
        for name in ("auth.json", "models.json", "models-store.json"):
            with open(os.path.join(self.pi_agent, name), "w") as f:
                f.write("{}")
        with open(os.path.join(self.pi_agent, "settings.json"), "w") as f:  # 不应被链接进作业的 agent 目录
            f.write('{"packages": ["git:example/pkg"]}')
        self.oc_global = os.path.join(self.tmp, "ocglobal.json")
        with open(self.oc_global, "w") as f:
            json.dump({"mcp": {"gsvc": {"type": "local", "command": ["g-run", "--x=1"]}}}, f)
        with open(os.path.join(self.work, "opencode.json"), "w") as f:  # 工程级配置也是只读来源
            json.dump({"mcp": {"psvc": {"type": "local", "command": ["p-run"]}}}, f)
        self.inject_env = {"AGENT_DISPATCH_PI_AGENT_DIR": self.pi_agent, "AGENT_DISPATCH_OPENCODE_GLOBAL": self.oc_global}
        # 登记两个 MCP 服务（local 带凭据 env、remote 带凭据 headers）和一个技能
        self.ok("mcp", "add", "svc1", "--command", "run-a", "--env", "TOKEN=tok1", env_extra=self.inject_env)
        self.ok("mcp", "add", "svc2", "--url", "https://example.internal/sse", "--headers", "Authorization=Bearer zz",
                env_extra=self.inject_env)
        self.skill_dir = os.path.join(self.tmp, "skill-one")
        os.makedirs(self.skill_dir)
        with open(os.path.join(self.skill_dir, "SKILL.md"), "w") as f:
            f.write("---\ndescription: 注入测试技能\n---\n")
        self.ok("skills", "add", "skill1", self.skill_dir, env_extra=self.inject_env)
        # 命令行里带 {extra} 的后端，才能看到下发的参数
        with open(self.backends) as f:
            backends = json.load(f)
        backends["fakepi2"] = {"format": "pi", "session": "named", "default_model": "m",
                               "argv": [sys.executable, FAKE, "pi", "{session}", "{task}", "{model}", "{extra}"],
                               "resume_argv": [sys.executable, FAKE, "pi", "{session}", "{task}", "{model}", "{extra}"],
                               "attach": "fakepi2 --session-id {session}"}
        backends["fakeoc2"] = {"format": "opencode", "session": "assigned", "default_model": "m",
                               "argv": [sys.executable, FAKE, "opencode", "", "{task}", "{extra}"]}
        with open(self.backends, "w") as f:
            json.dump(backends, f)
        os.makedirs(os.path.join(self.home, "roles"))
        for role_id, fields in (("r1", {"backend": "fakepi2", "mcp": ["svc1"], "skills": ["skill1"]}),
                                ("r2", {"backend": "fakeoc2", "mcp": ["svc1", "svc2"], "skills": ["skill1"]}),
                                ("r0", {"backend": "fakepi2"})):
            with open(os.path.join(self.home, "roles", f"{role_id}.json"), "w") as f:
                json.dump({"title": f"角色{role_id}", **fields}, f)

    def test_pi_managed_strict_injection(self):
        self.ok("start", "ij1", "--role", "r1", "--cwd", self.work, "INJECTDUMP\nSTATUS: PASS", env_extra=self.inject_env)
        self.ok("wait", "ij1", "--timeout", "20")
        report = self.job("ij1")["report"]
        agent = os.path.join(self.home, "jobs", "ij1", "inject", "pi-agent")
        self.assertEqual(report["PI_DIR"], agent)
        argv = json.loads(report["ARGV"])
        for flag in ("--no-skills", "--no-approve"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--skill") + 1], self.skill_dir)
        # 临时 agent 目录：mcp.json 只含选中的服务（600 权限），其余文件符号链接回源目录
        with open(os.path.join(agent, "mcp.json")) as f:
            servers = json.load(f)["mcpServers"]
        self.assertEqual(list(servers), ["svc1"])
        self.assertEqual(servers["svc1"]["env"]["TOKEN"], "tok1")
        self.assertEqual(os.stat(os.path.join(agent, "mcp.json")).st_mode & 0o777, 0o600)
        self.assertTrue(os.path.islink(os.path.join(agent, "models.json")))
        self.assertEqual(os.path.realpath(os.path.join(agent, "models.json")), os.path.join(self.pi_agent, "models.json"))
        self.assertFalse(os.path.exists(os.path.join(agent, "settings.json")))  # 扩展包配置不带进来
        self.assertEqual(self.job("ij1")["inject"],
                         {"mcp": [{"id": "svc1", "name": "svc1"}],
                          "skills": [{"id": "skill1", "name": "skill-one", "path": self.skill_dir}]})
        self.assertIn("svc1", self.ok("status", "ij1", env_extra=self.inject_env).stdout)

    def test_unmanaged_command_unchanged(self):
        self.ok("start", "ij0", "--role", "r0", "--cwd", self.work, "INJECTDUMP\nSTATUS: PASS", env_extra=self.inject_env)
        self.ok("wait", "ij0", "--timeout", "20")
        report = self.job("ij0")["report"]
        self.assertEqual(report["PI_DIR"], "-")
        self.assertNotIn("--no-skills", report["ARGV"])
        self.assertIsNone(self.job("ij0")["inject"])
        self.assertFalse(os.path.exists(os.path.join(self.home, "jobs", "ij0", "inject")))
        status = self.ok("status", "ij0", env_extra=self.inject_env).stdout
        self.assertNotIn("下发      ", status)

    def test_opencode_managed_env_config(self):
        self.ok("start", "ij2", "--role", "r2", "--cwd", self.work, "INJECTDUMP\nSTATUS: PASS", env_extra=self.inject_env)
        self.ok("wait", "ij2", "--timeout", "20")
        report = self.job("ij2")["report"]
        config = json.loads(report["OC_CFG"])
        self.assertIn("--standalone", report["ARGV"])
        self.assertEqual(config["skills"]["paths"], [self.skill_dir])
        mcp = config["mcp"]
        self.assertEqual(mcp["svc1"]["command"], ["run-a"])
        self.assertTrue(mcp["svc1"]["enabled"])
        self.assertEqual(mcp["svc2"]["url"], "https://example.internal/sse")
        # 工程与全局配置里有、没选中的服务：同名完整定义 + enabled:false
        self.assertEqual((mcp["psvc"]["command"], mcp["psvc"]["enabled"]), (["p-run"], False))
        self.assertEqual((mcp["gsvc"]["command"], mcp["gsvc"]["enabled"]), (["g-run", "--x=1"], False))
        path = os.path.join(self.home, "jobs", "ij2", "inject", "opencode-config.json")
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        status = self.ok("status", "ij2", env_extra=self.inject_env).stdout
        self.assertIn("无法排除执行者自己的全局技能", status)

    def test_takeover_command_and_cleanup(self):
        self.ok("start", "ij3", "--role", "r1", "--cwd", self.work, "STATUS: PASS", env_extra=self.inject_env)
        self.ok("wait", "ij3", "--timeout", "20")
        status = self.ok("status", "ij3", env_extra=self.inject_env).stdout
        agent = os.path.join(self.home, "jobs", "ij3", "inject", "pi-agent")
        self.assertIn(f"接手      agent-dispatch take ij3 之后：cd ", status)
        self.assertIn(f"env PI_CODING_AGENT_DIR={agent} fakepi2 --session-id", status)
        self.assertIn(f"--skill {self.skill_dir} --no-approve", status)
        self.ok("accept", "ij3")
        self.assertFalse(os.path.exists(os.path.join(self.home, "jobs", "ij3", "inject")))  # 凭据文件已清理
        self.ok("start", "ij4", "--role", "r2", "--cwd", self.work, "STATUS: PASS", env_extra=self.inject_env)
        self.ok("wait", "ij4", "--timeout", "20")
        self.ok("stop", "ij4")
        self.assertFalse(os.path.exists(os.path.join(self.home, "jobs", "ij4", "inject")))

    def test_dry_run_shows_injection(self):
        result = self.ok("start", "ij5", "--role", "r1", "--cwd", self.work, "任务", "--dry-run", "--json",
                         env_extra=self.inject_env)
        preview = json.loads(result.stdout)
        self.assertTrue(preview["inject"]["managed"])
        self.assertEqual(preview["inject"]["mcp"]["services"], [{"id": "svc1", "name": "svc1"}])
        self.assertIn("严格模式", preview["inject"]["mcp"]["note"])
        plain = self.ok("start", "ij5", "--role", "r0", "--cwd", self.work, "任务", "--dry-run", env_extra=self.inject_env).stdout
        self.assertNotIn("本次下发", plain)
        managed = self.ok("start", "ij5", "--role", "r1", "--cwd", self.work, "任务", "--dry-run",
                          env_extra=self.inject_env).stdout
        self.assertIn("本次下发", managed)
        self.assertIn("svc1", managed)
        unmanaged = json.loads(self.ok("start", "ij5", "--role", "r0", "--cwd", self.work, "任务",
                                       "--dry-run", "--json", env_extra=self.inject_env).stdout)["inject"]
        self.assertFalse(unmanaged["managed"])
        self.assertIn("未管理", unmanaged["note"])

    def test_merge_tmux_opencode_config(self):
        sys.path.insert(0, ROOT)
        from agent_dispatch import inject
        argv = ["env", 'OPENCODE_CONFIG_CONTENT={"model": "m"}', "opencode", "--session", "s"]
        env = inject.merge_tmux_opencode(argv, {"OPENCODE_CONFIG_CONTENT": '{"mcp": {"a": {"enabled": true}}}'})
        self.assertEqual(env, {})
        self.assertEqual(json.loads(argv[1].split("=", 1)[1]), {"model": "m", "mcp": {"a": {"enabled": True}}})
        self.assertEqual(inject.merge_tmux_opencode(argv, {}), {})


if __name__ == "__main__":
    unittest.main()


class ModelLimitTest(DispatchBase):
    """模型并发上限：默认值、单独覆盖、满员排队与自动开始、并发不超发、接口与命令行读写。"""

    def start_async(self, *args):
        """并发派工用：不等命令结束，返回子进程。"""
        env = dict(os.environ, AGENT_DISPATCH_HOME=self.home, AGENT_DISPATCH_BACKENDS=self.backends, PYTHONPATH=ROOT)
        for key in ("AGENT_DISPATCH_WORKER", "AGENT_DISPATCH_SESSION", "AGENT_DISPATCH_SESSION_KIND",
                    "CLAUDE_CODE_SESSION_ID", "TMUX_PANE", "TERM_SESSION_ID", "TMUX"):
            env.pop(key, None)
        return subprocess.Popen([sys.executable, "-m", "agent_dispatch", *args], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def test_default_limit_queues_and_auto_starts(self):
        self.ok("models", "limit", "--default", "1")
        self.ok("start", "j1", "-p", "t", "SLEEP 3\nSTATUS: PASS")
        result = self.ok("start", "j2", "-p", "t", "STATUS: PASS")
        self.assertIn("排队", result.stderr + result.stdout)
        job = self.job("j2")
        self.assertEqual(job["state"], "queued")
        self.assertTrue(job["waiting_models"])
        self.assertEqual(job["waiting_models"][0]["backend"], "fakepi")
        out = self.ok("status").stdout
        self.assertIn("排队中", out)
        self.assertIn("等 fakepi:", out)  # 总览里写明在等哪些模型
        detail = self.ok("status", "j2").stdout
        self.assertIn("排队", detail)
        waiting = self.cli("wait", "j2", "--timeout", "1")
        self.assertEqual(waiting.returncode, 3)  # 排队中的作业 wait 行为与运行中一致
        self.ok("wait", "j2", "--timeout", "40")
        self.assertEqual(self.job("j2")["status"], "PASS")
        self.assertEqual(self.job("j2")["state"], "done")

    def test_per_model_limit_overrides_default(self):
        self.make_project()
        self.make_pool("qa", "fa=50", "fb=50")
        self.ok("models", "limit", "--default", "10")
        self.ok("models", "limit", "fa", "1")
        self.ok("start", "j1", "-P", "p1", "-r", "qa", "--repo", "app", "SLEEP 3\nSTATUS: PASS")
        self.assertEqual(self.job("j1")["model_alias"], "fa")
        self.ok("start", "j2", "-P", "p1", "-r", "qa", "--repo", "app", "STATUS: PASS")  # fa 满了，按比例换 fb
        self.assertEqual(self.job("j2")["model_alias"], "fb")
        self.ok("wait", "j1", "--timeout", "30")
        self.ok("wait", "j2", "--timeout", "30")

    def test_pool_all_full_queues(self):
        self.make_project()
        self.make_pool("qa", "fa=50", "fb=50")
        self.ok("models", "limit", "--default", "1")  # fa、fb 都限 1
        self.ok("start", "j1", "-P", "p1", "-r", "qa", "--repo", "app", "SLEEP 3\nSTATUS: PASS")
        self.ok("start", "j2", "-P", "p1", "-r", "qa", "--repo", "app", "SLEEP 2\nSTATUS: PASS")
        self.ok("start", "j3", "-P", "p1", "-r", "qa", "--repo", "app", "STATUS: PASS")
        self.assertEqual(self.job("j3")["state"], "queued")
        self.ok("wait", "j1", "--timeout", "40")
        self.ok("wait", "j2", "--timeout", "40")
        self.ok("wait", "j3", "--timeout", "40")
        # 先排先得：j3 开始时 j2 已经结束
        self.assertLessEqual(self.job("j2")["turns"][-1]["ended"], self.job("j3")["turns"][0]["started"])

    def test_two_aliases_same_model_share_limit(self):
        self.make_project()
        for alias, model_id in (("fa", "m-a"), ("fa2", "m-a"), ("fb", "m-b")):
            self.ok("models", "add", alias, "--backend", "fakepi", "--id", model_id)
        self.ok("roles", "pool", "qa", "-P", "p1", "fa=1", "fa2=1")
        self.ok("models", "limit", "--default", "10")
        self.ok("models", "limit", "fa", "1")
        self.ok("start", "j1", "-P", "p1", "-r", "qa", "--repo", "app", "SLEEP 3\nSTATUS: PASS")
        self.ok("start", "j2", "-P", "p1", "-r", "qa", "--repo", "app", "STATUS: PASS")
        # fa2 虽是另一个短名，但指向同一个模型 ID，上限共用：j2 只能排队
        self.assertEqual(self.job("j2")["state"], "queued")
        self.ok("stop", "j1")
        self.ok("stop", "j2")

    def test_concurrent_starts_do_not_oversubscribe(self):
        self.ok("models", "limit", "--default", "2")
        procs = [self.start_async("start", f"c{i}", "-p", "t", "--channels", "none", "SLEEP 4\nSTATUS: PASS") for i in range(6)]
        for proc in procs:
            proc.wait(timeout=60)
        jobs = {j["name"]: j for j in json.loads(self.ok("status", "--json").stdout)}
        running = [j for j in jobs.values() if j["state"] == "running"]
        queued = [j for j in jobs.values() if j["state"] == "queued"]
        self.assertEqual(len(running), 2)
        self.assertEqual(len(queued), 4)
        for name in jobs:
            self.ok("wait", name, "--timeout", "90")

    def test_stop_queued_job(self):
        self.ok("models", "limit", "--default", "1")
        self.ok("start", "j1", "-p", "t", "SLEEP 6\nSTATUS: PASS")
        self.ok("start", "j2", "-p", "t", "STATUS: PASS")
        self.assertEqual(self.job("j2")["state"], "queued")
        self.ok("stop", "j2")
        self.assertEqual(self.job("j2")["state"], "stopped")
        self.ok("wait", "j1", "--timeout", "40")
        self.assertEqual(self.job("j2")["state"], "stopped")  # 不会被误启动

    def test_reply_queues_when_model_full(self):
        self.ok("models", "limit", "--default", "2")
        self.ok("start", "j1", "-p", "t", "--channels", "none", "SLEEP 12\nSTATUS: PASS")
        self.ok("start", "j2", "-p", "t", "--channels", "none", "STATUS: PASS")
        self.ok("wait", "j2", "--timeout", "20")  # j2 先占第二个名额跑完
        self.ok("start", "j3", "-p", "t", "--channels", "none", "SLEEP 12\nSTATUS: PASS")  # 再把名额占满
        result = self.ok("reply", "j2", "再干一轮\nSTATUS: PASS")
        self.assertIn("排队", result.stderr + result.stdout)
        job = self.job("j2")
        self.assertEqual(job["state"], "queued")
        self.assertEqual(job["turn"], 2)
        self.assertEqual(job["model"], "m")  # 续派不换模型
        self.ok("wait", "j1", "--timeout", "60")
        self.ok("wait", "j3", "--timeout", "60")
        self.ok("wait", "j2", "--timeout", "60")
        job = self.job("j2")
        self.assertEqual(job["state"], "done")
        self.assertEqual(job["turn"], 2)
        self.assertEqual(job["status"], "PASS")

    def test_cli_limit_io(self):
        self.ok("models", "add", "fa", "--backend", "fakepi", "--id", "m-a")
        self.ok("models", "limit", "fa", "3")
        self.assertIn("0/3", self.ok("models").stdout)
        self.ok("models", "limit", "--default", "2")
        self.ok("models", "limit", "fa", "default")
        self.assertIn("0/2", self.ok("models").stdout)  # 恢复默认后用全局默认值
        self.assertNotEqual(self.cli("models", "limit", "fa", "0").returncode, 0)
        self.assertNotEqual(self.cli("models", "limit", "fa", "abc").returncode, 0)
        self.assertNotEqual(self.cli("models", "limit", "nope", "2").returncode, 0)
        self.assertNotEqual(self.cli("models", "limit", "--default", "0").returncode, 0)
        # 占用数会出现在并发列里
        self.ok("models", "limit", "--default", "5")
        self.ok("start", "j1", "-p", "t", "-m", "m-a", "SLEEP 2\nSTATUS: PASS")
        self.assertIn("1/5", self.ok("models").stdout)
        self.ok("wait", "j1", "--timeout", "30")

    def test_web_limit_api(self):
        import socket, urllib.error, urllib.request
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        base = f"http://127.0.0.1:{port}"

        def call(method, path, body=None):
            request = urllib.request.Request(base + path, method=method, data=json.dumps(body or {}).encode())
            request.add_header("Content-Type", "application/json")
            request.add_header("X-Agent-Dispatch", "1")
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    return response.status, json.load(response)
            except urllib.error.HTTPError as exc:
                return exc.code, json.load(exc)

        try:
            self.ok("web", "start", "--port", str(port))
            self.assertEqual(call("POST", "/api/models", {"alias": "fa", "backend": "fakepi", "id": "m-a"})[0], 200)
            _, page = call("GET", "/api/models")
            self.assertEqual(page["default_max_concurrent"], 5)
            fa = next(m for m in page["models"] if m["alias"] == "fa")
            self.assertEqual(fa["limit"], 5)
            self.assertEqual(fa["limit_source"], "default")
            self.assertEqual(fa["running"], 0)
            self.assertEqual(call("POST", "/api/models/fa", {"max_concurrent": 2})[0], 200)
            _, page = call("GET", "/api/models")
            fa = next(m for m in page["models"] if m["alias"] == "fa")
            self.assertEqual(fa["limit"], 2)
            self.assertEqual(fa["limit_source"], "model")
            self.assertEqual(call("POST", "/api/models/fa", {"max_concurrent": None})[0], 200)
            self.assertEqual(call("POST", "/api/models/fa", {"max_concurrent": 0})[0], 400)
            self.assertEqual(call("POST", "/api/models/settings", {"default_max_concurrent": 3})[0], 200)
            _, page = call("GET", "/api/models")
            self.assertEqual(page["default_max_concurrent"], 3)
            fa = next(m for m in page["models"] if m["alias"] == "fa")
            self.assertEqual(fa["limit"], 3)  # 恢复默认后跟着默认值走
            # 排队中的作业在总览接口里带着在等的模型
            self.ok("models", "limit", "--default", "1")
            self.ok("start", "j1", "-p", "t", "SLEEP 3\nSTATUS: PASS")
            self.ok("start", "j2", "-p", "t", "STATUS: PASS")
            _, overview = call("GET", "/api/overview")
            j2 = next(j for j in overview["jobs"] if j["name"] == "j2")
            self.assertEqual(j2["state"], "queued")
            self.assertEqual(j2["waiting_models"][0]["backend"], "fakepi")
            self.ok("stop", "j1")
            self.ok("stop", "j2")
        finally:
            self.ok("web", "stop")

    def fake_pi_on_path(self):
        """放一个假的 pi 可执行文件到 PATH：models discover 只认 PATH 上的 pi。"""
        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(bindir, exist_ok=True)
        path = os.path.join(bindir, "pi")
        with open(path, "w") as f:
            f.write("#!/bin/sh\necho 'PROVIDER MODEL'\necho 'provA modelX'\necho 'provB modelY'\necho 'provA modelZ'\n")
        os.chmod(path, 0o755)
        return {"PATH": bindir + os.pathsep + os.environ["PATH"]}

    def test_models_import_list_and_named(self):
        env = self.fake_pi_on_path()
        out = self.ok("models", "import", "--list", env_extra=env).stdout
        self.assertIn("provA/modelX", out)
        self.assertIn("未登记", out)
        result = self.ok("models", "import", "provA/modelX", env_extra=env)
        self.assertIn("pi:modelX", result.stderr + result.stdout)
        out = self.ok("models", "import", "--list", env_extra=env).stdout
        self.assertIn("已登记（pi:modelX）", out)
        self.assertIn("未登记", out)  # 其余候选仍未登记
        again = self.ok("models", "import", "provA/modelX", env_extra=env)
        self.assertIn("（都已登记过）", again.stderr + again.stdout)
        self.assertIn("pi:modelX", self.ok("models").stdout)
        self.assertNotEqual(self.cli("models", "import", "no/such", env_extra=env).returncode, 0)
        # 原有的全量导入行为保留（限定后端）
        self.ok("models", "import", env_extra=env)
        self.assertIn("pi:modelY", self.ok("models").stdout)

    def test_web_models_import_candidates(self):
        import socket, urllib.error, urllib.request
        env = self.fake_pi_on_path()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        base = f"http://127.0.0.1:{port}"

        def call(method, path, body=None):
            request = urllib.request.Request(base + path, method=method, data=json.dumps(body or {}).encode())
            request.add_header("Content-Type", "application/json")
            request.add_header("X-Agent-Dispatch", "1")
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    return response.status, json.load(response)
            except urllib.error.HTTPError as exc:
                return exc.code, json.load(exc)

        try:
            self.ok("web", "start", "--port", str(port), env_extra=env)
            _, page = call("GET", "/api/models/candidates?backend=pi")
            ids = {c["id"] for c in page["candidates"]}
            self.assertIn("provA/modelX", ids)
            self.assertTrue(all(not c["registered"] for c in page["candidates"] if c["backend"] == "pi"))
            status, result = call("POST", "/api/models/import", {"names": ["provA/modelX"], "backend": "pi"})
            self.assertEqual(status, 200)
            self.assertEqual(result["added"], ["pi:modelX"])
            _, page = call("GET", "/api/models/candidates?backend=pi")
            self.assertTrue(next(c["registered"] for c in page["candidates"] if c["backend"] == "pi" and c["id"] == "provA/modelX"))
        finally:
            self.ok("web", "stop")


class RoleEnvLimitTest(DispatchBase):
    """环境维度、角色继承、角色并发上限、回合结束即释放、按环境下发 MCP。字段都不写时行为与原先一样（其余用例覆盖）。"""

    def setUp(self):
        super().setUp()
        self.make_project()
        self.env2_dir = os.path.join(self.tmp, "app-e2")
        os.makedirs(os.path.join(self.env2_dir, "runs"))
        self.ok("projects", "add-repo", "p1", f"app2={self.env2_dir}")
        self.project_file = os.path.join(self.home, "projects", "p1", "project.json")
        with open(self.project_file) as f:
            data = json.load(f)
        data["envs"] = {"e1": {"name": "环境一"}, "e2": {"vars": {"AT_ENV": "test-e2"}}}
        data["limit_groups"] = {"test": 1}
        with open(self.project_file, "w") as f:
            json.dump(data, f)

    def role(self, role_id, **fields):
        with open(os.path.join(self.home, "projects", "p1", "roles", f"{role_id}.json"), "w") as f:
            json.dump(fields, f, ensure_ascii=False)

    def test_extends_inherits_fields_and_brief(self):
        self.role("qa-line", extends="qa", title="托盘主测试", repos=["app"])
        self.ok("start", "x1", "-P", "p1", "-r", "托盘主测试", "STATUS: PASS")
        self.ok("wait", "x1", "--timeout", "20")
        job = self.job("x1")
        self.assertEqual((job["role"], job["role_title"], job["backend"]), ("qa-line", "托盘主测试", "fakepi"))
        with open(os.path.join(self.home, "jobs", "x1", "brief.md")) as f:
            self.assertIn("你是托盘主测试", f.read())  # 模板沿用 qa 的，称呼是自己的
        self.assertNotIn("没有配置闸门", self.cli("gate", "x1").stderr)  # 闸门也继承
        shown = self.ok("roles", "show", "qa-line", "-P", "p1").stdout
        self.assertIn("托盘主测试", shown)

    def test_extends_cycle_and_missing_base_rejected(self):
        self.role("a", extends="b", repos=["app"])
        self.role("b", extends="a", repos=["app"])
        result = self.cli("start", "x1", "-P", "p1", "-r", "a", "STATUS: PASS")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("成环", result.stderr)
        self.role("c", extends="nope", repos=["app"])
        result = self.cli("start", "x2", "-P", "p1", "-r", "c", "STATUS: PASS")
        self.assertIn("不存在的角色", result.stderr)

    def test_env_required_validated_and_passed_to_executor(self):
        self.role("t-line", extends="qa", title="线测试", needs_env=True, repo_by_env={"e1": "app", "e2": "app2"})
        missing = self.cli("start", "x1", "-P", "p1", "-r", "t-line", "STATUS: PASS")
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("--env", missing.stderr)
        unknown = self.cli("start", "x1", "-P", "p1", "-r", "t-line", "--env", "e9", "STATUS: PASS")
        self.assertIn("没有环境", unknown.stderr)
        self.ok("start", "x1", "-P", "p1", "-r", "t-line", "--env", "e2", "INJECTDUMP\nSTATUS: PASS")
        self.ok("wait", "x1", "--timeout", "20")
        job = self.job("x1")
        self.assertEqual((job["env"], job["cwd"], job["repo"]), ("e2", self.env2_dir, "app2"))  # 环境决定工程
        self.assertEqual(job["report"]["AD_ENV"], "e2")
        self.assertEqual(job["report"]["AT_ENV"], "test-e2")  # 项目给这个环境配的额外变量
        self.assertIn("环境      e2", self.ok("status", "x1").stdout)
        self.ok("start", "x0", "-P", "p1", "-r", "dev", "INJECTDUMP\nSTATUS: PASS")  # 不带环境的角色照旧
        self.ok("wait", "x0", "--timeout", "20")
        self.assertEqual(self.job("x0")["report"]["AD_ENV"], "-")

    def test_max_parallel_counts_per_role_and_env(self):
        self.role("t-line", extends="qa", title="线测试", needs_env=True, max_parallel=1,
                  repo_by_env={"e1": "app", "e2": "app2"}, channels="none")
        self.ok("start", "j1", "-P", "p1", "-r", "t-line", "--env", "e1", "SLEEP 4\nSTATUS: PASS")
        queued = self.ok("start", "j2", "-P", "p1", "-r", "t-line", "--env", "e1", "STATUS: PASS")
        self.assertIn("角色并发已满", queued.stderr)
        job = self.job("j2")
        self.assertEqual(job["state"], "queued")
        self.assertIn("线测试", job["waiting_limit"])
        self.assertIn("j1", job["waiting_limit"])
        self.assertIn("线测试", self.ok("status").stdout)
        self.ok("start", "j3", "-P", "p1", "-r", "t-line", "--env", "e2", "STATUS: PASS")  # 另一个环境不受影响
        self.assertNotEqual(self.job("j3")["state"], "queued")
        self.ok("wait", "j1", "--timeout", "30")
        self.ok("wait", "j2", "--timeout", "30")  # j1 结束后自动开始
        self.assertEqual((self.job("j2")["state"], self.job("j2")["status"]), ("done", "PASS"))
        self.assertLessEqual(self.job("j1")["turns"][-1]["ended"], self.job("j2")["turns"][0]["started"])
        self.ok("wait", "j3", "--timeout", "30")
        # 没写上限的角色不受限
        self.ok("start", "d1", "-P", "p1", "-r", "dev", "SLEEP 2\nSTATUS: PASS")
        self.ok("start", "d2", "-P", "p1", "-r", "dev", "SLEEP 2\nSTATUS: PASS")
        self.assertNotEqual(self.job("d2")["state"], "queued")
        self.ok("wait", "d1", "--timeout", "30")
        self.ok("wait", "d2", "--timeout", "30")

    def test_limit_group_caps_roles_together(self):
        for role_id in ("t-a", "t-b"):
            self.role(role_id, extends="qa", title=f"测试{role_id}", repos=["app"], channels="none",
                      max_parallel=1, limit_group="test")
        self.ok("start", "g1", "-P", "p1", "-r", "t-a", "SLEEP 4\nSTATUS: PASS")
        self.ok("start", "g2", "-P", "p1", "-r", "t-b", "STATUS: PASS")  # 角色自己有空位，但分组合计上限 1
        job = self.job("g2")
        self.assertEqual(job["state"], "queued")
        self.assertIn("分组 test", job["waiting_limit"])
        self.ok("stop", "g2")  # 排队中的可以直接叫停
        self.assertEqual(self.job("g2")["state"], "stopped")
        self.ok("start", "g3", "-P", "p1", "-r", "t-b", "STATUS: PASS")
        self.ok("wait", "g1", "--timeout", "30")
        self.ok("wait", "g3", "--timeout", "30")
        self.assertEqual(self.job("g3")["status"], "PASS")

    @unittest.skipUnless(shutil.which("tmux"), "需要 tmux")
    def test_release_on_done_closes_session_and_frees_slot(self):
        self.role("t-line", extends="qa", title="线测试", repos=["app"], channels="all", mode="tmux",
                  max_parallel=1, release_on_done=True)
        self.ok("start", "r1", "-P", "p1", "-r", "t-line", "STATUS: PASS")
        self.ok("start", "r2", "-P", "p1", "-r", "t-line", "STATUS: PASS")
        self.assertEqual(self.job("r2")["state"], "queued")
        self.ok("wait", "r1", "--timeout", "40")
        job = self.job("r1")
        self.assertEqual((job["state"], job["status"]), ("done", "PASS"))
        # 回合一结束界面就关了（没等验收），通道与位置都还回来，排队的 r2 自动开始
        self.assertNotEqual(subprocess.run(["tmux", "has-session", "-t", "=ad-r1"], capture_output=True).returncode, 0)
        self.ok("wait", "r2", "--timeout", "40")
        self.assertEqual(self.job("r2")["status"], "PASS")
        # 返工：同一个会话 ID 重开界面，再跑一轮后又关掉
        self.ok("reply", "r1", "STATUS: FAIL")
        self.ok("wait", "r1", "--timeout", "40")
        job = self.job("r1")
        self.assertEqual((job["turn"], job["state"], job["status"]), (2, "done", "FAIL"))
        self.assertNotEqual(subprocess.run(["tmux", "has-session", "-t", "=ad-r1"], capture_output=True).returncode, 0)

    @unittest.skipUnless(shutil.which("tmux"), "需要 tmux")
    def test_tmux_without_release_keeps_slot_until_accept(self):
        self.role("t-line", extends="qa", title="线测试", repos=["app"], channels="none", mode="tmux",
                  max_parallel=1, gates=[])
        self.ok("start", "k1", "-P", "p1", "-r", "t-line", "STATUS: PASS")
        self.ok("wait", "k1", "--timeout", "40")
        self.ok("start", "k2", "-P", "p1", "-r", "t-line", "STATUS: PASS")
        self.assertEqual(self.job("k2")["state"], "queued")  # 界面还开着，位置仍被 k1 占着
        self.ok("accept", "k1")
        self.ok("wait", "k2", "--timeout", "40")  # 验收关界面后自动补位
        self.assertEqual(self.job("k2")["status"], "PASS")

    def test_mcp_selected_by_env_with_placeholders(self):
        pi_agent = os.path.join(self.tmp, "piagent-src")
        os.makedirs(os.path.join(pi_agent, "sessions"))
        for name in ("auth.json", "models.json", "settings.json"):
            with open(os.path.join(pi_agent, name), "w") as f:
                f.write("{}")
        env = {"AGENT_DISPATCH_PI_AGENT_DIR": pi_agent}
        self.ok("mcp", "add", "db@e1", "--name", "db", "--command", "db-run", "--env", "DSN=mysql://u:p1@h/e1", env_extra=env)
        self.ok("mcp", "add", "db@e2", "--name", "db", "--command", "db-run", "--env", "DSN=mysql://u:p2@h/e2",
                "--exposure", "direct", env_extra=env)
        self.ok("mcp", "add", "pw-pc", "--command", "pw-run",
                "--args=--user-data-dir,/tmp/pw-{{project}}-{{env}}-{{role}}-pc", env_extra=env)
        with open(self.backends) as f:
            backends = json.load(f)
        backends["fakepi2"] = {"format": "pi", "session": "named", "default_model": "m",
                               "argv": [sys.executable, FAKE, "pi", "{session}", "{task}", "{model}", "{extra}"],
                               "resume_argv": [sys.executable, FAKE, "pi", "{session}", "{task}", "{model}", "{extra}"]}
        with open(self.backends, "w") as f:
            json.dump(backends, f)
        self.role("t-line", extends="qa", title="线测试", repos=["app"], channels="none", backend="fakepi2",
                  needs_env=True, mcp=["db@{{env}}", "pw-pc"])
        self.ok("start", "m1", "-P", "p1", "-r", "t-line", "--env", "e2", "INJECTDUMP\nSTATUS: PASS", env_extra=env)
        self.ok("wait", "m1", "--timeout", "20")
        job = self.job("m1")
        self.assertEqual([s["id"] for s in job["inject"]["mcp"]], ["db@e2", "pw-pc"])
        agent = job["report"]["PI_DIR"]
        with open(os.path.join(agent, "mcp.json")) as f:
            servers = json.load(f)["mcpServers"]
        self.assertEqual(sorted(servers), ["db", "pw-pc"])
        self.assertEqual(servers["db"]["env"]["DSN"], "mysql://u:p2@h/e2")
        self.assertEqual(servers["db"]["exposure"], "direct")  # 暴露方式原样带给 pi
        self.assertNotIn("exposure", servers["pw-pc"])
        self.assertEqual(servers["pw-pc"]["args"], ["--user-data-dir", "/tmp/pw-p1-e2-t-line-pc"])
        # 只管 MCP 不管技能：用户的设置照常链接进来（重试策略、扩展包不丢），也不强行忽略工程自己的 .pi 资源
        self.assertTrue(os.path.islink(os.path.join(agent, "settings.json")))
        self.assertNotIn("--no-approve", json.loads(job["report"]["ARGV"]))
        # 环境可选的角色没带 --env：按环境选的服务不下发，其余照常
        self.role("t-opt", extends="qa", title="环境可选", repos=["app"], channels="none", backend="fakepi2",
                  mcp=["db@{{env}}", "pw-pc"])
        started = self.ok("start", "m3", "-P", "p1", "-r", "t-opt", "INJECTDUMP\nSTATUS: PASS", env_extra=env)
        self.assertIn("本次不下发", started.stderr)
        self.ok("wait", "m3", "--timeout", "20")
        with open(os.path.join(self.job("m3")["report"]["PI_DIR"], "mcp.json")) as f:
            self.assertEqual(list(json.load(f)["mcpServers"]), ["pw-pc"])
        # 空列表 = 一个服务都不挂
        self.role("t-none", extends="qa", title="无服务", repos=["app"], channels="none", backend="fakepi2", mcp=[])
        self.ok("start", "m2", "-P", "p1", "-r", "t-none", "INJECTDUMP\nSTATUS: PASS", env_extra=env)
        self.ok("wait", "m2", "--timeout", "20")
        with open(os.path.join(self.job("m2")["report"]["PI_DIR"], "mcp.json")) as f:
            self.assertEqual(json.load(f)["mcpServers"], {})


class WebVersionTest(DispatchBase):
    """代码版本标识：磁盘上的 agent_dispatch/*.py 比进程启动时新，页面和 web status 都要能发现。"""

    def test_code_version_changes_when_file_changes(self):
        """版本摘要跟文件内容走：改动、新增 py 文件后摘要必须变。"""
        from agent_dispatch import web
        directory = os.path.join(self.tmp, "pkg")
        os.makedirs(directory)
        with open(os.path.join(directory, "a.py"), "w", encoding="utf-8") as f:
            f.write("x = 1\n")
        first = web.code_version(directory)
        self.assertRegex(first, r"^[0-9a-f]{10}$")
        # 内容变了（大小也变）→ 摘要变
        with open(os.path.join(directory, "a.py"), "w", encoding="utf-8") as f:
            f.write("x = 1\n" + "#" * 200 + "\n")
        self.assertNotEqual(first, web.code_version(directory))
        # 新增文件也能反映
        with open(os.path.join(directory, "b.py"), "w", encoding="utf-8") as f:
            f.write("y = 2\n")
        self.assertNotEqual(first, web.code_version(directory))

    def test_overview_carries_both_versions_and_status_warns(self):
        """/api/version 与总览都带两个版本；磁盘比进程新时 web status 提示重启。"""
        import time as time_mod
        import urllib.request
        from agent_dispatch import web
        port = self.start_web()
        base = f"http://127.0.0.1:{port}"
        # 改 backends.py 的修改时间（不动内容）模拟「磁盘比进程新」，测完恢复
        target = os.path.join(ROOT, "agent_dispatch", "backends.py")
        saved = os.stat(target)
        bumped = (saved.st_atime_ns, saved.st_mtime_ns + 1_000_000_000)
        try:
            with urllib.request.urlopen(base + "/api/version", timeout=5) as response:
                info = json.load(response)
            self.assertTrue(info["process"] and info["disk"])
            self.assertEqual(info["process"], info["disk"])  # 刚启动：两边一致
            with urllib.request.urlopen(base + "/api/overview", timeout=5) as response:
                self.assertEqual(json.load(response)["versions"], info)
            os.utime(target, ns=bumped)
            time_mod.sleep(web.VERSION_TTL + 0.3)  # 等服务端磁盘版本的短缓存过期
            with urllib.request.urlopen(base + "/api/version", timeout=5) as response:
                stale = json.load(response)
            self.assertNotEqual(stale["process"], stale["disk"])
            self.assertIn("旧版本", self.ok("web", "status").stdout)
        finally:
            os.utime(target, ns=(saved.st_atime_ns, saved.st_mtime_ns))
            self.ok("web", "stop")
