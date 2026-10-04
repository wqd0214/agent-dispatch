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
  waiting_models?: { backend: string; id: string; alias: string | null }[]
  batch_title?: string
  batch_reason?: string
  follows?: string[]
  dispatcher?: { kind: string; session_id: string; title: string | null } | null
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
  versions?: { process: string; disk: string }  // 两者不同：服务进程还是旧代码，需重启
  jobs: JobSummary[]
  cases: { tc: string; verdict: string; evidence: string; note: string; job: string; batch: string; group: string; project: string; run: string }[]
  channels: { group: string; project: string; profile: string; table: Record<string, string | null> }[]
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

export interface BatchSummary {
  id: string
  title: string
  reason: string
  created: string | null
  dispatcher: { kind: string; session_id: string; title: string }
  counts: { total: number; running: number; review: number; done: number; failed: number }
  activity: string
  jobs: string[]
}

export interface BatchesData { now: string; batches: BatchSummary[] }

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
  followed_by?: string[]
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
  batch_title?: string
  batch_reason?: string
  follows?: string[]
  job?: string
  task?: string
}

// ---------- 配置中心与模型登记（/api/config、/api/models）----------

export interface RoleField<T = unknown> { value: T; source: string }

export interface RoleBrief { text: string | null; source: string | null; path: string | null }

export interface RoleDetail {
  role: string
  title: string
  scope: string
  repos: string[]
  pool_source: string | null
  fields: Record<string, RoleField>
  brief: RoleBrief
}

export interface ConfigProject {
  id: string
  name: string
  default_role: string | null
  defaults: Record<string, unknown>
  supervisor: string
  repos: { id: string; name: string; cwd: string; channels: string[]; channel_ns: string }[]
  roles: { id: string; title: string; description: string; repos: string[]; scope: string }[]
}

export interface ConfigOverview {
  projects: ConfigProject[]
  roles: { id: string; title: string; description: string }[]
}

export interface PoolEntry { model: string; weight: number }

export interface RegisteredModel {
  alias: string
  backend: string
  id: string
  enabled: boolean
  note: string
  paused: { reason: string; until: number } | null
  used_by: { project: string; project_name: string; role: string; role_title: string; weight: number }[]
  jobs: number
  settled: number
  pass: number
  failed: number
  avg_seconds: number | null
  provider_failures: number
  backend_enabled: boolean
  running: number
  limit: number
  limit_source: 'default' | 'model'
  missing?: boolean
}

export interface ToolInfo {
  name: string
  enabled: boolean
  default_model: string | null
  models: number
}

export interface ModelPage {
  cooldown_min: number
  default_max_concurrent: number
  models: RegisteredModel[]
  backends: string[]
  tools: ToolInfo[]
}

/** 工具里现有的模型（导入前先看再选用） */
export interface ModelCandidate {
  backend: string
  id: string
  registered: boolean
  alias: string | null
}

// ---------- MCP 与技能登记表（/api/mcp、/api/skills）----------

export interface RegistryUser { project: string; role: string }

/** 接口给的 MCP 条目已是遮罩后的：改时原样提交 •••• 即保留原值 */
export interface McpEntry {
  id: string
  name: string
  type?: 'local' | 'remote'
  command?: string
  args?: string[]
  env?: Record<string, string>
  url?: string
  headers?: Record<string, string>
  description?: string
  sources?: string[]
  used_by: RegistryUser[]
}

export interface SkillEntry {
  id: string
  name: string
  path: string
  paths?: string[]
  description?: string
  sources?: string[]
  used_by: RegistryUser[]
}

export interface CandidateVariant {
  id: string
  name: string
  registered: boolean
  source: string
  sources?: string[]
  path?: string
  description?: string
  diff?: Record<string, unknown>
  entry?: Record<string, unknown>
}

export interface CandidateGroup { name: string; conflict: boolean; variants: CandidateVariant[] }
