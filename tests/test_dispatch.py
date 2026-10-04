"""端到端测试：用假智能体走完整的命令行流程，不依赖真实的 pi / opencode。"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAKE = os.path.join(ROOT, "tests", "fake_agent.py")
FAKE_TUI = os.path.join(ROOT, "tests", "fake_tui.py")


class DispatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="ad-test-"))
        self.home = os.path.join(self.tmp, "home")
        self.work = os.path.join(self.tmp, "work")
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
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cli(self, *args, stdin=None, cwd=None):
        env = dict(os.environ, AGENT_DISPATCH_HOME=self.home, AGENT_DISPATCH_BACKENDS=self.backends, PYTHONPATH=ROOT)
        env.pop("AGENT_DISPATCH_WORKER", None)
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

    def make_project(self):
        """项目 p1（显示名 产品线A）：两个工程 app / web；角色 dev（专属，负责 app）、qa（覆盖全局，负责两个工程）。"""
        self.web_dir = os.path.join(self.tmp, "web-repo")
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

    def make_pool(self, role, *pairs):
        for alias, model_id in (("fa", "m-a"), ("fb", "m-b")):
            if alias not in self.ok("models").stdout:
                self.ok("models", "add", alias, "--backend", "fakepi", "--id", model_id)
        self.ok("roles", "pool", role, "-P", "p1", *pairs)

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

    def test_dry_run_prints_brief(self):
        out = self.ok("start", "j15", "-p", "t", "--dry-run", "跑场景A").stdout
        self.assertIn("派工单 j15 通道 a、b", out)
        self.assertIn("跑场景A", out)
        self.assertEqual(self.jobs(), [])


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


if __name__ == "__main__":
    unittest.main()
