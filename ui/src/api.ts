// 带类型的接口封装。写请求统一带 X-Agent-Dispatch: 1（服务端用它挡跨站请求）。
import type { DispatchOptions, JobActionBody, JobDetail, LogChunk, Overview } from './types'

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
  dispatch: (body: JobActionBody) => post('/api/jobs', body),
  jobAction: (name: string, action: 'reply' | 'stop' | 'gate' | 'accept' | 'reject' | 'clear-escalation', body: unknown = {}) =>
    post(`/api/jobs/${encodeURIComponent(name)}/${action}`, body),
}
