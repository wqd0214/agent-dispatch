// 真实扩展 harness：用 Node 原生类型剥离直接执行 .pi/extensions/agent-dispatch-supervisor.ts，
// 验证 idle/busy 投递、去重、抑制、重载恢复、会话切换、关停清理与 worker/print/开关不监听。
// 每个场景独立模块实例（工厂闭包）与独立临时目录；结果以 "RESULT {...}" 行输出，供 Python 断言。
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

const extensionPath = path.resolve(process.argv[2]);
const args = process.argv.slice(3);

function argOf(name) {
  const i = args.indexOf(name);
  return i >= 0 ? args[i + 1] : undefined;
}
const boundaryHome = argOf("--boundary-home");
const boundarySession = argOf("--boundary-session");
const boundaryEvent = argOf("--boundary-event");

const results = [];

function record(scenario, ok, detail = "") {
  results.push({ scenario, ok: Boolean(ok), detail: String(detail) });
  console.log("RESULT " + JSON.stringify({ scenario, ok: Boolean(ok), detail: String(detail) }));
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function makeCtx(session, mode = "tui") {
  return {
    mode,
    hasUI: mode === "tui",
    sessionManager: { getSessionId: () => session },
    ui: { notify() {} },
  };
}

function makePi() {
  const handlers = new Map();
  const sends = [];
  return {
    sends,
    api: {
      on(name, handler) {
        if (!handlers.has(name)) handlers.set(name, []);
        handlers.get(name).push(handler);
        return () => {};
      },
      sendMessage(message, options) {
        sends.push({ message, options: options ?? {} });
      },
    },
    async fire(name, event, ctx) {
      for (const handler of handlers.get(name) ?? []) await handler(event, ctx);
    },
  };
}

function tempHome(name) {
  return fs.mkdtempSync(path.join(os.tmpdir(), "ad-ext-" + name + "-"));
}

function sessionSubdir(homeDir, session) {
  return path.join(homeDir, "notify", "pi", session.replace(/[^A-Za-z0-9._-]/g, "_"));
}

function writeEvent(homeDir, session, event) {
  const dir = sessionSubdir(homeDir, session);
  fs.mkdirSync(dir, { recursive: true });
  const file = path.join(dir, event.event_id + ".json");
  fs.writeFileSync(file, JSON.stringify(event));
  return file;
}

function readDir(homeDir, session) {
  const dir = sessionSubdir(homeDir, session);
  if (!fs.existsSync(dir)) return { pending: [], sent: [], suppressed: [] };
  const files = fs.readdirSync(dir);
  return {
    pending: files.filter((f) => f.endsWith(".json") && !f.endsWith(".sent.json") && !f.endsWith(".suppressed.json")),
    sent: files.filter((f) => f.endsWith(".sent.json")),
    suppressed: files.filter((f) => f.endsWith(".suppressed.json")),
  };
}

async function waitFor(fn, what, timeout = 5000) {
  const deadline = Date.now() + timeout;
  let last;
  while (Date.now() < deadline) {
    last = fn();
    if (last) return last;
    await sleep(50);
  }
  throw new Error("等待超时：" + what);
}

let factoryCache;
async function load() {
  if (!factoryCache) {
    const mod = await import(extensionPath);
    factoryCache = mod.default;
  }
  if (typeof factoryCache !== "function") throw new Error("扩展没有导出默认工厂函数");
  return factoryCache;
}

function baseEvent(over = {}) {
  return {
    event_id: "pi-harness-t1",
    job: "harness",
    turn: 1,
    session_id: "s-harness",
    state: "done",
    status: "PASS",
    message: "[agent-dispatch] 作业 harness 第 1 轮已结束：待审查，STATUS=PASS",
    ts: "2026-10-23 12:00:00",
    ...over,
  };
}

const SESSION = "11111111-2222-4333-8444-555555555555";

async function scenarioIdle() {
  const homeDir = tempHome("idle");
  process.env.AGENT_DISPATCH_HOME = homeDir;
  try {
    const factory = await load();
    const pi = makePi();
    factory(pi.api);
    await pi.fire("session_start", { type: "session_start", reason: "startup" }, makeCtx(SESSION));
    writeEvent(homeDir, SESSION, baseEvent({ session_id: SESSION }));
    await waitFor(() => pi.sends.length >= 1, "idle 投递");
    const send = pi.sends[0];
    const marker = path.join(sessionSubdir(homeDir, SESSION), "pi-harness-t1.sent.json");
    const consumed = JSON.parse(fs.readFileSync(marker, "utf-8")).consumed;
    // 统一 followUp 请求：闲时由 pi 的 triggerTurn 立即启动新回合，标记如实记录 followUp
    const ok = send.options.triggerTurn === true && send.options.deliverAs === "followUp"
      && send.message.customType === "agent-dispatch-result"
      && send.message.display === true
      && String(send.message.content).includes("harness")
      && send.message.details?.event_id === "pi-harness-t1"
      && consumed.deliverAs === "followUp";
    record("idle_trigger_turn", ok, JSON.stringify(send.options) + " consumed=" + JSON.stringify(consumed));
  } finally {
    delete process.env.AGENT_DISPATCH_HOME;
    fs.rmSync(homeDir, { recursive: true, force: true });
  }
}

// pi 生命周期窗口回归：运行启动中（agent_start 尚未发出）、agent_end 后（续跑/压缩/重试可能再起 run）。
// 扩展不维护私有 busy，三个窗口的断言一致：统一 triggerTurn + followUp；
// 若将来恢复 busy 分叉逻辑，这些场景会抓住窗口错位。
async function expectFollowUpAt(homeDir, factory, label, warmup) {
  const pi = makePi();
  factory(pi.api);
  await pi.fire("session_start", { type: "session_start", reason: "startup" }, makeCtx(SESSION));
  if (warmup) await warmup(pi);
  const eventId = "pi-w-" + label;
  writeEvent(homeDir, SESSION, baseEvent({ event_id: eventId, session_id: SESSION }));
  await waitFor(() => pi.sends.length >= 1, label + " 投递");
  const send = pi.sends[0];
  const ok = send.options.triggerTurn === true && send.options.deliverAs === "followUp"
    && readDir(homeDir, SESSION).sent.length === 1;
  record(label, ok, JSON.stringify(send.options));
}

async function scenarioRunStartingWindow() {
  const homeDir = tempHome("win-start");
  process.env.AGENT_DISPATCH_HOME = homeDir;
  try {
    await expectFollowUpAt(homeDir, await load(), "window_run_starting_follow_up", null);
  } finally {
    delete process.env.AGENT_DISPATCH_HOME;
    fs.rmSync(homeDir, { recursive: true, force: true });
  }
}

async function scenarioBusy() {
  const homeDir = tempHome("busy");
  process.env.AGENT_DISPATCH_HOME = homeDir;
  try {
    await expectFollowUpAt(homeDir, await load(), "busy_follow_up",
      async (pi) => pi.fire("agent_start", { type: "agent_start" }, makeCtx(SESSION)));
  } finally {
    delete process.env.AGENT_DISPATCH_HOME;
    fs.rmSync(homeDir, { recursive: true, force: true });
  }
}

async function scenarioAfterAgentEnd() {
  const homeDir = tempHome("win-end");
  process.env.AGENT_DISPATCH_HOME = homeDir;
  try {
    await expectFollowUpAt(homeDir, await load(), "window_after_agent_end_follow_up",
      async (pi) => {
        await pi.fire("agent_start", { type: "agent_start" }, makeCtx(SESSION));
        await pi.fire("agent_end", { type: "agent_end", messages: [] }, makeCtx(SESSION));
      });
  } finally {
    delete process.env.AGENT_DISPATCH_HOME;
    fs.rmSync(homeDir, { recursive: true, force: true });
  }
}

// effective_state 跨语言等价回归（与 tests/test_dispatch.py 的
// test_pi_effective_state_matches_extension_contract 同一组输入）：
// raw running + runner 已死/缺失 => interrupted（送入）；runner 存活 => running（抑制）
async function scenarioEffectiveState() {
  const homeDir = tempHome("eff");
  process.env.AGENT_DISPATCH_HOME = homeDir;
  try {
    const factory = await load();
    const pi = makePi();
    factory(pi.api);
    await pi.fire("session_start", { type: "session_start", reason: "startup" }, makeCtx(SESSION));
    const jobFile = path.join(homeDir, "job.json");
    const child = (await import("node:child_process")).spawn("/bin/sleep", ["30"]);
    child.kill();
    await new Promise((resolve) => child.on("exit", resolve));
    await new Promise((resolve) => setTimeout(resolve, 100));
    let dead;
    try {
      process.kill(child.pid, 0);
      record("effective_state_equivalence", false, "子进程 pid 意外仍存活，无法构造已死 pid");
      return;
    } catch (error) {
      if (error.code !== "ESRCH") throw error;
      dead = child.pid;
    }
    // case A：runner 已死 => interrupted，送入
    fs.writeFileSync(jobFile, JSON.stringify({ name: "h", turn: 1, state: "running", runner_pid: dead }));
    writeEvent(homeDir, SESSION, baseEvent({ event_id: "pi-e-dead", session_id: SESSION, job_file: jobFile }));
    await waitFor(() => readDir(homeDir, SESSION).sent.length >= 1, "已死 runner 送入");
    const deadSent = pi.sends.length === 1 && pi.sends[0].options.deliverAs === "followUp";
    // case B：runner 存活（本进程）=> running，抑制
    fs.writeFileSync(jobFile, JSON.stringify({ name: "h", turn: 1, state: "running", runner_pid: process.pid }));
    writeEvent(homeDir, SESSION, baseEvent({ event_id: "pi-e-alive", session_id: SESSION, job_file: jobFile }));
    await waitFor(() => readDir(homeDir, SESSION).suppressed.length >= 1, "存活 runner 抑制");
    const alive = JSON.parse(fs.readFileSync(
      path.join(sessionSubdir(homeDir, SESSION), "pi-e-alive.suppressed.json"), "utf-8"));
    const aliveOk = pi.sends.length === 1 && /running/.test(alive.consumed?.reason || "");
    // case C：runner_pid 缺失 => interrupted，送入
    fs.writeFileSync(jobFile, JSON.stringify({ name: "h", turn: 1, state: "running" }));
    writeEvent(homeDir, SESSION, baseEvent({ event_id: "pi-e-missing", session_id: SESSION, job_file: jobFile }));
    await waitFor(() => pi.sends.length >= 2, "缺失 runner_pid 送入");
    const missingOk = pi.sends.length === 2 && pi.sends[1].options.deliverAs === "followUp";
    record("effective_state_equivalence", deadSent && aliveOk && missingOk,
      "dead=" + deadSent + " aliveReason=" + alive.consumed?.reason + " missing=" + missingOk);
  } finally {
    delete process.env.AGENT_DISPATCH_HOME;
    fs.rmSync(homeDir, { recursive: true, force: true });
  }
}

async function scenarioDuplicate() {
  const homeDir = tempHome("dup");
  process.env.AGENT_DISPATCH_HOME = homeDir;
  try {
    const factory = await load();
    const pi = makePi();
    factory(pi.api);
    await pi.fire("session_start", { type: "session_start", reason: "startup" }, makeCtx(SESSION));
    const file = writeEvent(homeDir, SESSION, baseEvent({ session_id: SESSION }));
    fs.writeFileSync(file, JSON.stringify(baseEvent({ session_id: SESSION }))); // 同一事件再触发一次 watch
    await waitFor(() => pi.sends.length >= 1, "去重投递");
    await sleep(500);
    const state = readDir(homeDir, SESSION);
    record("duplicate_event", pi.sends.length === 1 && state.sent.length === 1 && state.pending.length === 0,
      "sends=" + pi.sends.length + " dir=" + JSON.stringify(state));
  } finally {
    delete process.env.AGENT_DISPATCH_HOME;
    fs.rmSync(homeDir, { recursive: true, force: true });
  }
}

async function scenarioSuppressed() {
  const homeDir = tempHome("sup");
  process.env.AGENT_DISPATCH_HOME = homeDir;
  try {
    const factory = await load();
    const pi = makePi();
    factory(pi.api);
    await pi.fire("session_start", { type: "session_start", reason: "startup" }, makeCtx(SESSION));
    const jobFile = path.join(homeDir, "job.json");
    fs.writeFileSync(jobFile, JSON.stringify({ name: "h", turn: 2, state: "done" }));
    writeEvent(homeDir, SESSION, baseEvent({ event_id: "pi-s-t1", turn: 1, session_id: SESSION, job_file: jobFile }));
    await waitFor(() => readDir(homeDir, SESSION).suppressed.length >= 1, "过时事件被抑制");
    const staleOk = pi.sends.length === 0;
    fs.writeFileSync(jobFile, JSON.stringify({ name: "h", turn: 1, state: "accepted" }));
    writeEvent(homeDir, SESSION, baseEvent({ event_id: "pi-s-t2", turn: 1, session_id: SESSION, job_file: jobFile }));
    await waitFor(() => readDir(homeDir, SESSION).suppressed.length >= 2, "已裁决事件被抑制");
    const accepted = JSON.parse(fs.readFileSync(
      path.join(sessionSubdir(homeDir, SESSION), "pi-s-t2.suppressed.json"), "utf-8"));
    const decidedOk = pi.sends.length === 0 && /验收|accepted/.test(accepted.consumed?.reason || "");
    record("stale_and_decided_suppressed", staleOk && decidedOk,
      "sends=" + pi.sends.length + " reason=" + accepted.consumed?.reason);
  } finally {
    delete process.env.AGENT_DISPATCH_HOME;
    fs.rmSync(homeDir, { recursive: true, force: true });
  }
}

async function scenarioReload() {
  const homeDir = tempHome("reload");
  process.env.AGENT_DISPATCH_HOME = homeDir;
  try {
    const factory = await load();
    const first = makePi();
    factory(first.api);
    await first.fire("session_start", { type: "session_start", reason: "startup" }, makeCtx(SESSION));
    await first.fire("session_shutdown", { type: "session_shutdown", reason: "reload" }, makeCtx(SESSION));
    // 重载后遗留的未消费事件要由新运行时补上
    writeEvent(homeDir, SESSION, baseEvent({ event_id: "pi-r-t1", session_id: SESSION }));
    const second = makePi();
    factory(second.api);
    await second.fire("session_start", { type: "session_start", reason: "reload" }, makeCtx(SESSION));
    await waitFor(() => second.sends.length >= 1, "重载后补投遗留事件");
    const state = readDir(homeDir, SESSION);
    record("reload_recovery", first.sends.length === 0 && second.sends.length === 1 && state.pending.length === 0,
      "first=" + first.sends.length + " second=" + second.sends.length + " pending=" + state.pending.length);
  } finally {
    delete process.env.AGENT_DISPATCH_HOME;
    fs.rmSync(homeDir, { recursive: true, force: true });
  }
}

async function scenarioSwitch() {
  const homeDir = tempHome("switch");
  process.env.AGENT_DISPATCH_HOME = homeDir;
  const oldSession = "22222222-2222-4333-8444-555555555555";
  try {
    const factory = await load();
    const before = makePi();
    factory(before.api);
    await before.fire("session_start", { type: "session_start", reason: "startup" }, makeCtx(oldSession));
    await before.fire("session_shutdown", { type: "session_shutdown", reason: "resume" }, makeCtx(oldSession));
    const after = makePi();
    factory(after.api);
    await after.fire("session_start", { type: "session_start", reason: "resume" }, makeCtx(SESSION));
    // 切换后的会话只消费自己的事件；旧会话的遗留事件原样保留，不误发到新会话
    writeEvent(homeDir, oldSession, baseEvent({ event_id: "pi-sw-old", session_id: oldSession }));
    writeEvent(homeDir, SESSION, baseEvent({ event_id: "pi-sw-new", session_id: SESSION }));
    await waitFor(() => after.sends.length >= 1, "新会话消费自己的事件");
    await sleep(400);
    const oldDir = readDir(homeDir, oldSession);
    const ok = after.sends.length === 1 && after.sends[0].message.details?.session_id === SESSION
      && oldDir.pending.length === 1 && oldDir.sent.length === 0 && oldDir.suppressed.length === 0;
    record("session_switch", ok, "old=" + JSON.stringify(oldDir) + " sends=" + JSON.stringify(after.sends.map((s) => s.message.details?.session_id)));
  } finally {
    delete process.env.AGENT_DISPATCH_HOME;
    fs.rmSync(homeDir, { recursive: true, force: true });
  }
}

async function scenarioShutdown() {
  const homeDir = tempHome("shutdown");
  process.env.AGENT_DISPATCH_HOME = homeDir;
  try {
    const factory = await load();
    const pi = makePi();
    factory(pi.api);
    await pi.fire("session_start", { type: "session_start", reason: "startup" }, makeCtx(SESSION));
    await pi.fire("session_shutdown", { type: "session_shutdown", reason: "quit" }, makeCtx(SESSION));
    writeEvent(homeDir, SESSION, baseEvent({ session_id: SESSION }));
    await sleep(600);
    const state = readDir(homeDir, SESSION);
    record("shutdown_cleanup", pi.sends.length === 0 && state.pending.length === 1,
      "sends=" + pi.sends.length + " dir=" + JSON.stringify(state));
  } finally {
    delete process.env.AGENT_DISPATCH_HOME;
    fs.rmSync(homeDir, { recursive: true, force: true });
  }
}

async function scenarioGuarded(label, name, setup, mode = "tui") {
  const homeDir = tempHome(name);
  const restore = typeof setup === "function" ? setup() : null;
  process.env.AGENT_DISPATCH_HOME = homeDir;
  try {
    const factory = await load();
    const pi = makePi();
    factory(pi.api);
    await pi.fire("session_start", { type: "session_start", reason: "startup" }, makeCtx(SESSION, mode));
    writeEvent(homeDir, SESSION, baseEvent({ session_id: SESSION }));
    await sleep(600);
    const state = readDir(homeDir, SESSION);
    record(label, pi.sends.length === 0 && state.pending.length === 1,
      "sends=" + pi.sends.length + " dir=" + JSON.stringify(state));
  } finally {
    if (restore) restore();
    delete process.env.AGENT_DISPATCH_HOME;
    fs.rmSync(homeDir, { recursive: true, force: true });
  }
}

async function scenarioBoundary() {
  const saved = process.env.AGENT_DISPATCH_HOME;
  process.env.AGENT_DISPATCH_HOME = boundaryHome;
  try {
    const factory = await load();
    const pi = makePi();
    factory(pi.api);
    await pi.fire("session_start", { type: "session_start", reason: "startup" }, makeCtx(boundarySession));
    const send = await waitFor(() => pi.sends.find((s) => s.message.details?.event_id === boundaryEvent),
      "消费 Python 产出的事件");
    const state = readDir(boundaryHome, boundarySession);
    const ok = String(send.message.content).includes(boundaryEvent) && state.pending.length === 0
      && state.sent.length === 1;
    record("boundary_real_event", ok, JSON.stringify(send.message.details));
  } finally {
    if (saved === undefined) delete process.env.AGENT_DISPATCH_HOME;
    else process.env.AGENT_DISPATCH_HOME = saved;
  }
}

async function main() {
  const steps = [
    ["idle_trigger_turn", scenarioIdle],
    ["window_run_starting_follow_up", scenarioRunStartingWindow],
    ["busy_follow_up", scenarioBusy],
    ["window_after_agent_end_follow_up", scenarioAfterAgentEnd],
    ["effective_state_equivalence", scenarioEffectiveState],
    ["duplicate_event", scenarioDuplicate],
    ["stale_and_decided_suppressed", scenarioSuppressed],
    ["reload_recovery", scenarioReload],
    ["session_switch", scenarioSwitch],
    ["shutdown_cleanup", scenarioShutdown],
    ["worker_no_listen", () => scenarioGuarded("worker_no_listen", "worker",
      () => {
        process.env.AGENT_DISPATCH_WORKER = "j-x";
        return () => delete process.env.AGENT_DISPATCH_WORKER;
      })],
    ["print_no_listen", () => scenarioGuarded("print_no_listen", "print", null, "print")],
    ["switch_off_no_listen", () => scenarioGuarded("switch_off_no_listen", "off",
      () => {
        process.env.AGENT_DISPATCH_PI_NOTIFY = "0";
        return () => delete process.env.AGENT_DISPATCH_PI_NOTIFY;
      })],
  ];
  if (boundaryHome && boundarySession && boundaryEvent) {
    steps.unshift(["boundary_real_event", scenarioBoundary]);
  }
  for (const [name, scenario] of steps) {
    try {
      await scenario();
    } catch (error) {
      record(name, false, error && error.stack ? String(error.stack).split("\n").slice(0, 4).join(" | ") : String(error));
    }
  }
  process.exit(results.every((r) => r.ok) ? 0 : 1);
}

await main();
