// 带类型的接口封装。写请求统一带 X-Agent-Dispatch: 1（服务端用它挡跨站请求）。
import type { BatchesData, DispatchOptions, JobActionBody, JobDetail, LogChunk, Overview } from './types'

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function getJson<T>(url: string): Promise<T> {
  const response = await fetch(url)
  if (!response.ok) throw new ApiError(response.status, await errText(response))
  return response.json() as Promise<T>
}

async function post<T = { ok: boolean; message?: string; error?: string }>(url: string, body?: unknown): Promise<T> {
  const response = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Agent-Dispatch': '1' },
    body: JSON.stringify(body ?? {}),
  })
  if (!response.ok) throw new ApiError(response.status, await errText(response))
  return response.json() as Promise<T>
}

async function errText(response: Response): Promise<string> {
  try {
    const data = await response.json()
    return data.error || data.message || `请求失败（${response.status}）`
  } catch {
    return `请求失败（${response.status}）`
  }
}

export const api = {
  overview: () => getJson<Overview>('/api/overview'),
  jobDetail: (name: string) => getJson<JobDetail>(`/api/job/${encodeURIComponent(name)}`),
  jobLog: (name: string, offset: number) =>
    getJson<LogChunk>(`/api/job/${encodeURIComponent(name)}/log?offset=${offset}`),
  jobFileUrl: (name: string, path: string) => `/api/job/${encodeURIComponent(name)}/file?path=${encodeURIComponent(path)}`,
  dispatchOptions: () => getJson<DispatchOptions>('/api/dispatch-options'),
  dispatchPreview: (body: JobActionBody) => post<Record<string, unknown>>('/api/jobs/preview', body),
  dispatch: (body: JobActionBody) => post<{ ok: boolean; job?: string; message?: string }>('/api/jobs', body),
  jobAction: (name: string, action: 'reply' | 'stop' | 'gate' | 'accept' | 'reject' | 'clear-escalation', body: unknown = {}) =>
    post<{ ok: boolean; passed?: boolean; message?: string }>(`/api/jobs/${encodeURIComponent(name)}/${action}`, body),
}

export const batchApi = {
  list: () => getJson<BatchesData>('/api/batches'),
  update: (id: string, body: { title?: string; reason?: string }) =>
    post(`/api/batches/${encodeURIComponent(id)}`, body),
}

// ---------- 配置中心与模型登记 ----------
import type { ConfigOverview, ModelPage, PoolEntry, RoleDetail } from './types'

type PostResult = { ok: boolean; message?: string; error?: string }

async function del(url: string): Promise<PostResult> {
  const response = await fetch(url, { method: 'DELETE', headers: { 'X-Agent-Dispatch': '1' } })
  if (!response.ok) throw new ApiError(response.status, await errText(response))
  return response.json() as Promise<PostResult>
}

function projectQuery(project: string): string {
  return project ? `?project=${encodeURIComponent(project)}` : ''
}

export const configApi = {
  overview: () => getJson<ConfigOverview>('/api/config'),
  roleDetail: (roleId: string, project: string) =>
    getJson<RoleDetail>(`/api/config/roles/${encodeURIComponent(roleId)}${projectQuery(project)}`),
  addRole: (body: { id: string; project?: string; title?: string; description?: string; repos?: string[]; backend?: string; mode?: string }) =>
    post<{ ok: boolean; path: string; inherits: boolean }>('/api/config/roles', body),
  setRoleFields: (roleId: string, project: string, fields: Record<string, unknown>) =>
    post(`/api/config/roles/${encodeURIComponent(roleId)}`, { project, fields }),
  resetRoleFields: (roleId: string, project: string, reset: string[]) =>
    post(`/api/config/roles/${encodeURIComponent(roleId)}`, { project, reset }),
  setBrief: (roleId: string, project: string, text: string) =>
    post(`/api/config/roles/${encodeURIComponent(roleId)}/brief`, { project, text }),
  resetBrief: (roleId: string, project: string) =>
    post(`/api/config/roles/${encodeURIComponent(roleId)}/brief`, { project, reset: true }),
  deleteRole: (roleId: string, project: string) =>
    del(`/api/config/roles/${encodeURIComponent(roleId)}${projectQuery(project)}`),
}

export const modelApi = {
  page: () => getJson<ModelPage>('/api/models'),
  importFrom: (backend: string) => post<{ added: string[] }>('/api/models/import', { backend }),
  add: (body: { alias: string; backend: string; id: string; note: string }) => post('/api/models', body),
  update: (alias: string, body: { enabled?: boolean; note?: string }) =>
    post(`/api/models/${encodeURIComponent(alias)}`, body),
  resume: (alias: string) => post(`/api/models/${encodeURIComponent(alias)}/resume`),
  remove: (alias: string) => del(`/api/models/${encodeURIComponent(alias)}`),
  setBackendEnabled: (name: string, enabled: boolean) => post(`/api/backends/${encodeURIComponent(name)}`, { enabled }),
  savePool: (role: string, project: string, pool: PoolEntry[] | null) => post('/api/pools', { project, role, pool }),
}
