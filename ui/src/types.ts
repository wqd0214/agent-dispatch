// 与 agent_dispatch/web.py 返回结构对应的类型定义

export interface JobSummary {
  name: string
  batch: string
  profile: string
  group: string          // 项目名（历史命名）
  project: string        // 工程名（历史命名）
  role: string
  role_title: string
  backend: string
  model: string
  model_alias: string | null
  model_actual: string | null
  state: string
  state_label: string
  status: string | null
  turn: number
  seconds: number | null
  channels: string[]
  task: string
  needs_user: string | null
  reason: string | null
  gate_ok: boolean | null
  created: string
  mode: string
  updated: string
  ended: string | null
  activity: string
}

export interface RoleDef {
  id: string
  title: string
  description: string
  repos: string[]
  backend: string
  mode: string
  scope: string
}

export interface ProjectDef {
  id: string
  name: string
  roles: RoleDef[]
  repos: { id: string; name: string; cwd: string; channels: string[]; channel_ns: string }[]
}

export interface Overview {
  now: string
  home: string
  jobs: JobSummary[]
  cases: { tc: string; verdict: string; evidence: string; note: string; job: string; batch: string; group: string; project: string; run: string }[]
  channels: { group: string; project: string; profile: string; table: unknown }[]
  definitions: ProjectDef[]
}

export interface JobEvent {
  kind: 'tool' | 'text' | 'error'
  t: string
  turn: number
  name?: string
  input?: unknown
  output?: unknown
  error?: boolean
  ms?: number | null
  text?: string
  message?: string
}

export interface GateCheck {
  gate: string
  level: string
  ok: boolean
  detail: string
}

export interface Gate {
  at: string
  turn: number
  ok: boolean
  checks: GateCheck[]
}

export interface RunFile { path: string; size: number }

export interface JobDetail extends JobSummary {
  cwd: string
  session_id: string | null
  attach: unknown
  tmux_alive: boolean
  report: Record<string, string>
  gate: Gate | null
  decisions: { at: string; turn: number; action: string; note: string }[]
  turns: { turn: number; started: string; ended: string; rc: number; state: string; status: string | null; reason: string | null }[]
  brief: string
  result: string
  events: JobEvent[]
  run_dir: string | null
  run_files: RunFile[]
  job_dir: string
}

export interface LogChunk { offset: number; text: string }

export interface DispatchOptionProject {
  id: string
  name: string
  repos: { id: string; name: string }[]
  roles: { id: string; title: string; description: string; repos: string[]; pooled: boolean }[]
}

export interface DispatchOptions { projects: DispatchOptionProject[] }

export interface JobActionBody {
  project?: string
  role?: string
  repo?: string
  batch?: string
  job?: string
  task?: string
}
