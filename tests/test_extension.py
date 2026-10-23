"""真实 pi 扩展 harness：用 Node 直接执行 .pi/extensions/agent-dispatch-supervisor.ts。

验证 idle triggerTurn、busy followUp、重复事件、抑制、重载恢复、会话切换、
shutdown 清理，以及 worker / print / 关闭开关不监听；boundary 场景消费
Python 侧真实流程产出的事件文件，覆盖 Python ↔ TypeScript 边界，不是纯 mock。
"""
import json
import os
import shutil
import subprocess
import unittest

from test_dispatch import PI_SESSION_A, DispatchBase

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXTENSION = os.path.join(ROOT, ".pi", "extensions", "agent-dispatch-supervisor.ts")
HARNESS = os.path.join(ROOT, "tests", "extension_harness.mjs")

SCENARIOS = ("boundary_real_event", "idle_trigger_turn", "busy_follow_up", "duplicate_event",
             "stale_and_decided_suppressed", "reload_recovery", "session_switch", "shutdown_cleanup",
             "worker_no_listen", "print_no_listen", "switch_off_no_listen")


class ExtensionHarnessTest(DispatchBase):
    """执行真实的 TypeScript 扩展：监听、消费与生命周期都在 Node 里跑真代码。"""

    def setUp(self):
        super().setUp()
        self.assertTrue(os.path.isfile(EXTENSION), "扩展文件不存在：" + EXTENSION)

    def run_harness(self, boundary_home=None, boundary_session=None):
        cmd = [shutil.which("node"), HARNESS, EXTENSION]
        if boundary_home:
            cmd += ["--boundary-home", boundary_home, "--boundary-session", boundary_session]
        # 宿主（含真实 pi 会话）的身份与通知变量不能泄漏进 harness：扩展按环境决定是否监听
        env = dict(os.environ)
        for key in ("AGENT_DISPATCH_WORKER", "AGENT_DISPATCH_SESSION", "AGENT_DISPATCH_SESSION_KIND",
                    "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "CODEX_SESSION_ID", "PI_SESSION_ID",
                    "AGENT_DISPATCH_CODEX_NOTIFY", "AGENT_DISPATCH_PI_NOTIFY", "AGENT_DISPATCH_HOME",
                    "AGENT_DISPATCH_NOTIFY_POLL", "AGENT_DISPATCH_CODEX_QUEUE_TIMEOUT"):
            env.pop(key, None)
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300, cwd=ROOT, env=env)
        results = []
        for line in proc.stdout.splitlines():
            if line.startswith("RESULT "):
                results.append(json.loads(line[len("RESULT "):]))
        return proc, results

    @unittest.skipUnless(shutil.which("node"), "需要 node")
    def test_extension_scenarios_pass(self):
        proc, results = self.run_harness()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        names = [r["scenario"] for r in results]
        for expected in SCENARIOS:
            if expected == "boundary_real_event":
                continue
            self.assertIn(expected, names, proc.stdout + proc.stderr)
        failures = [r for r in results if not r.get("ok")]
        self.assertEqual(failures, [], json.dumps(failures, ensure_ascii=False, indent=2))

    @unittest.skipUnless(shutil.which("node"), "需要 node")
    def test_extension_consumes_real_python_event(self):
        # Python 流程真实产出事件文件，真实扩展代码消费它：边界两边都是被测实现
        pi_env = {"PI_SESSION_ID": PI_SESSION_A, "AGENT_DISPATCH_PI_NOTIFY": "1",
                  "AGENT_DISPATCH_NOTIFY_POLL": "0.1"}
        self.ok("start", "bx", "-p", "t", "MKRUN r1\nSTATUS: PASS\nRUN_DIR: runs/r1", env_extra=pi_env)
        self.ok("wait", "bx", "--timeout", "20")
        self.assertEqual(self.job("bx")["pi_notify"][0]["state"], "queued")
        proc, results = self.run_harness(boundary_home=self.home, boundary_session=PI_SESSION_A)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        result = next(r for r in results if r["scenario"] == "boundary_real_event")
        self.assertTrue(result["ok"], json.dumps(result, ensure_ascii=False))
        # 事件文件被改名为已送入标记，Python 侧 status 能看到
        with open(os.path.join(self.home, "notify", "pi", PI_SESSION_A, "pi-bx-t1.sent.json"),
                  encoding="utf-8") as f:
            consumed = json.load(f)["consumed"]
        self.assertEqual(consumed["deliverAs"], "triggerTurn")
        detail = self.ok("status", "bx").stdout
        self.assertIn("已送入会话", detail)
