/**
 * agent-dispatch 主管通知扩展：把本地派工结果送回当前 pi 主管会话。
 *
 * Python 侧（agent-dispatch）在一轮结束后把事件原子写入
 *   $AGENT_DISPATCH_HOME/notify/pi/<session>/<event_id>.json
 * 本扩展只监听当前会话自己的目录（session_start 建，session_shutdown 清）：
 *   - 空闲时 triggerTurn 触发回合，忙时排 followUp（pi 自己会在回合结束后投递）；
 *   - 送入前重查作业台账：过时、人工持有、叫停、已裁决的一律抑制并留下标记；
 *   - 去重靠内存 handled 集合 + 串行处理；未消费事件保留在磁盘，重载/恢复时补投；
 *   - 执行者（AGENT_DISPATCH_WORKER）与非交互单轮模式（print / json）不监听；
 *   - AGENT_DISPATCH_PI_NOTIFY=0 关闭。
 * 监听与等待只用文件系统，不调用模型。
 */
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const CUSTOM_TYPE = "agent-dispatch-result";
const DELIVERABLE = new Set(["done", "failed", "interrupted"]);
const DECIDED = new Set(["held", "stopped", "accepted", "rejected"]);

interface DispatchEvent {
  event_id: string;
  job: string;
  turn: number;
  session_id: string;
  state: string;
  status?: string | null;
  message: string;
  job_file?: string;
  ts?: string;
}

export default function (pi: ExtensionAPI) {
  let watcher: fs.FSWatcher | undefined;
  let dir: string | undefined;
  let busy = false;
  let draining: Promise<void> = Promise.resolve();
  const handled = new Set<string>();

  const enabled = () => process.env.AGENT_DISPATCH_PI_NOTIFY !== "0";

  function home(): string {
    return process.env.AGENT_DISPATCH_HOME || path.join(os.homedir(), ".agent-dispatch");
  }

  function dirFor(session: string): string {
    return path.join(home(), "notify", "pi", session.replace(/[^A-Za-z0-9._-]/g, "_"));
  }

  function stopWatching(): void {
    if (watcher) {
      watcher.close();
      watcher = undefined;
    }
  }

  function mark(file: string, suffix: ".sent.json" | ".suppressed.json", extra: Record<string, unknown>): void {
    // 先原子改名退出待消费集合，再把消费结果补写进新文件（状态可诊断）
    const target = file.replace(/\.json$/, suffix);
    try {
      fs.renameSync(file, target);
      const payload = JSON.parse(fs.readFileSync(target, "utf-8"));
      payload.consumed = { at: new Date().toISOString(), ...extra };
      fs.writeFileSync(target, JSON.stringify(payload, null, 2) + "\n");
    } catch {
      // 标记补写失败不影响主流程：文件已不在待消费集合里
    }
  }

  /** 送入前重查台账；返回 null 表示可以送入，否则返回抑制原因。 */
  function recheck(event: DispatchEvent): string | null {
    if (!event.job_file) {
      return null;
    }
    let job: { turn?: number; state?: string };
    try {
      job = JSON.parse(fs.readFileSync(event.job_file, "utf-8"));
    } catch {
      return "作业台账已不存在";
    }
    if (job.turn !== event.turn) {
      return `回合已过时（台账在第 ${job.turn} 轮）`;
    }
    if (typeof job.state === "string" && DECIDED.has(job.state)) {
      return `作业已${job.state}，不自动送入`;
    }
    if (typeof job.state !== "string" || !DELIVERABLE.has(job.state)) {
      return `作业状态为 ${job.state ?? "未知"}，暂不送入`;
    }
    return null;
  }

  function deliver(event: DispatchEvent): string {
    const message = {
      customType: CUSTOM_TYPE,
      content: event.message,
      display: true,
      details: event,
    };
    if (busy) {
      // 忙时排 followUp：当前回合结束后 pi 自动把它作为新一轮送入
      pi.sendMessage(message, { triggerTurn: true, deliverAs: "followUp" });
      return "followUp";
    }
    pi.sendMessage(message, { triggerTurn: true });
    return "triggerTurn";
  }

  function drain(): Promise<void> {
    const current = dir;
    draining = draining.then(async () => {
      if (!current) return;
      let files: string[] = [];
      try {
        files = fs.readdirSync(current).filter(
          (f) => f.endsWith(".json") && !f.endsWith(".sent.json") && !f.endsWith(".suppressed.json"),
        );
      } catch {
        return; // 目录还没建好或已消失
      }
      const pending: { file: string; event: DispatchEvent }[] = [];
      for (const file of files) {
        try {
          pending.push({ file, event: JSON.parse(fs.readFileSync(path.join(current, file), "utf-8")) });
        } catch {
          continue; // 半截写入，等下一次事件再读
        }
      }
      pending.sort((a, b) => (a.event.ts || "").localeCompare(b.event.ts || "")
        || String(a.event.event_id).localeCompare(String(b.event.event_id)));
      for (const { file, event } of pending) {
        if (!event || typeof event.event_id !== "string" || handled.has(event.event_id)) {
          continue;
        }
        handled.add(event.event_id);
        const reason = recheck(event);
        if (reason) {
          mark(path.join(current, file), ".suppressed.json", { action: "suppressed", reason });
          continue;
        }
        const how = deliver(event);
        mark(path.join(current, file), ".sent.json", { action: "sent", deliverAs: how });
      }
    });
    return draining;
  }

  pi.on("session_start", async (_event, ctx) => {
    stopWatching(); // 幂等：切换/重载前 session_shutdown 已清过，这里再兜底
    handled.clear();
    busy = false;
    dir = undefined;
    draining = Promise.resolve();
    if (!enabled() || process.env.AGENT_DISPATCH_WORKER) return;
    if (ctx.mode !== "tui" && ctx.mode !== "rpc") return; // 非交互单轮模式不开长期监听
    const session = ctx.sessionManager.getSessionId();
    if (!session) return;
    const target = dirFor(session);
    try {
      fs.mkdirSync(target, { recursive: true });
    } catch {
      return;
    }
    dir = target;
    watcher = fs.watch(target, () => {
      void drain();
    });
    await drain(); // 补投重载/恢复前遗留的未消费事件
  });

  pi.on("agent_start", () => {
    busy = true;
  });

  pi.on("agent_end", () => {
    busy = false;
  });

  pi.on("session_shutdown", async () => {
    stopWatching();
    dir = undefined;
    handled.clear();
    busy = false;
  });
}
