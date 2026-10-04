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

    def cli(self, *args, stdin=None, cwd=None, env_extra=None):
        env = dict(os.environ, AGENT_DISPATCH_HOME=self.home, AGENT_DISPATCH_BACKENDS=self.backends, PYTHONPATH=ROOT)
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

    def start_web(self):
        """起一个临时网页服务，返回 (port, 关闭函数)。"""
        import socket
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = str(sock.getsockname()[1])
        self.ok("web", "start", "--port", port)
        return port

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


class ConfigApiTest(DispatchTest):
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


class DispatcherSessionTest(DispatchTest):
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


if __name__ == "__main__":
    unittest.main()
