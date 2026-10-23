"""真实 pi 扩展 harness：用 Node 直接执行 .pi/extensions/agent-dispatch-supervisor.ts。

验证 idle/busy 各生命周期窗口统一 followUp、重复事件、抑制、effective_state 跨语言等价、
重载恢复、会话切换、shutdown 清理，以及 worker / print / 关闭开关不监听；boundary 场景消费
Python 侧真实流程产出的事件文件，覆盖 Python ↔ TypeScript 边界，不是纯 mock。
"""
import json
import os
import shutil
import unittest

from test_dispatch import EXTENSION, PI_SESSION_A, DispatchBase, run_extension_harness

SCENARIOS = ("boundary_real_event", "idle_trigger_turn", "window_run_starting_follow_up",
             "busy_follow_up", "window_after_agent_end_follow_up", "effective_state_equivalence",
             "duplicate_event", "stale_and_decided_suppressed", "reload_recovery", "session_switch",
             "shutdown_cleanup", "worker_no_listen", "print_no_listen", "switch_off_no_listen")


class ExtensionHarnessTest(DispatchBase):
    """执行真实的 TypeScript 扩展：监听、消费与生命周期都在 Node 里跑真代码。"""

    def setUp(self):
        super().setUp()
        self.assertTrue(os.path.isfile(EXTENSION), "扩展文件不存在：" + EXTENSION)

    def run_harness(self, boundary_home=None, boundary_session=None, boundary_event=None):
        return run_extension_harness(boundary_home, boundary_session, boundary_event)

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
        proc, results = self.run_harness(boundary_home=self.home, boundary_session=PI_SESSION_A,
                                         boundary_event="pi-bx-t1")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        result = next(r for r in results if r["scenario"] == "boundary_real_event")
        self.assertTrue(result["ok"], json.dumps(result, ensure_ascii=False))
        # 事件文件被改名为已送入标记，Python 侧 status 能看到；统一 followUp 请求
        with open(os.path.join(self.home, "notify", "pi", PI_SESSION_A, "pi-bx-t1.sent.json"),
                  encoding="utf-8") as f:
            consumed = json.load(f)["consumed"]
        self.assertEqual(consumed["deliverAs"], "followUp")
        detail = self.ok("status", "bx").stdout
        self.assertIn("已送入会话", detail)
