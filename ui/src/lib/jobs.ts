// 作业筛选、排序、状态标签的纯函数（可单测）

import type { JobSummary, ProjectDef } from '../types'

export interface JobFilters {
  states: string[]        // 状态多选，空 = 全部
  role: string            // 角色 id，空 = 全部
  batch: string           // 批次，空 = 全部
  project: string         // 工程（仓库）名，空 = 全部
  search: string          // 搜作业名和任务文本
}

export const EMPTY_FILTERS: JobFilters = { states: [], role: '', batch: '', project: '', search: '' }

/** 作业属于哪个项目：台账的 group 存项目名，可能与定义里的 id 或 name 相等 */
export function projectKeyOf(job: Pick<JobSummary, 'group'>): string {
  return job.group || ''
}

/** 某项目（按定义的 id 或 name）名下是否包含该作业 */
export function jobInProject(job: JobSummary, projectId: string, definitions: ProjectDef[]): boolean {
  if (!projectId) return true // 全部项目
  const def = definitions.find(d => d.id === projectId)
  return job.group === projectId || (!!def && (job.group === def.name || (!!def.repos.find(r => r.cwd) && def.repos.some(r => job.project === r.id))))
}

/** 待处理 = 等你决定 / 待审查 / 失败与中断；用于侧栏角标 */
export function needsAttention(job: JobSummary): boolean {
  return !!job.needs_user || ['done', 'failed', 'interrupted'].includes(job.state)
}

export function filterJobs(jobs: JobSummary[], filters: JobFilters): JobSummary[] {
  const q = filters.search.trim().toLowerCase()
  return jobs.filter(j => {
    if (filters.states.length && !filters.states.includes(j.state)) return false
    if (filters.role && j.role !== filters.role) return false
    if (filters.batch && j.batch !== filters.batch) return false
    if (filters.project && j.project !== filters.project) return false
    if (q && !j.name.toLowerCase().includes(q) && !(j.task || '').toLowerCase().includes(q)) return false
    return true
  })
}

/** 按最近活动排序：updated 新的在前；无 updated 的按 created 兜底 */
export function sortJobs(jobs: JobSummary[]): JobSummary[] {
  return [...jobs].sort((a, b) => (b.updated || b.created || '').localeCompare(a.updated || a.created || ''))
}

export interface TagStyle { label: string; type: 'primary' | 'success' | 'warning' | 'danger' | 'info' }

/** 状态 → 标签文字与 Element Plus 标签类型；待你决定优先于底层状态 */
export function stateTag(job: Pick<JobSummary, 'state' | 'state_label' | 'needs_user'>): TagStyle {
  if (job.needs_user) return { label: '待你决定', type: 'warning' }
  const map: Record<string, TagStyle['type']> = {
    running: 'primary', queued: 'info', done: 'warning', failed: 'danger', interrupted: 'danger',
    stopped: 'info', held: 'info', accepted: 'success', rejected: 'info',
  }
  return { label: queueText(job) ?? job.state_label ?? job.state, type: map[job.state] ?? 'info' }
}

/** 排队中的作业显示「排队中 · 等 <模型>」；非排队返回 null */
export function queueText(job: Pick<JobSummary, 'state' | 'waiting_models'>): string | null {
  if (job.state !== 'queued') return null
  const names = (job.waiting_models ?? []).map(w => `${w.backend}:${w.alias || w.id}`)
  return names.length ? `排队中 · 等 ${names.join('、')}` : '排队中'
}

/** 报告 STATUS → 标签 */
export function statusTag(status: string | null): TagStyle | null {
  if (!status) return null
  const map: Record<string, TagStyle['type']> = {
    PASS: 'success', FAIL: 'danger', BLOCKED: 'warning', NEEDS_DECISION: 'warning',
  }
  return { label: status, type: map[status] ?? 'info' }
}

/** 列表筛选器需要的选项（按当前项目里的作业算） */
export function filterOptions(jobs: JobSummary[]) {
  const roles = new Map<string, string>()
  const batches = new Set<string>()
  const projects = new Set<string>()
  for (const j of jobs) {
    if (j.role) roles.set(j.role, j.role_title || j.role)
    if (j.batch) batches.add(j.batch)
    if (j.project) projects.add(j.project)
  }
  return {
    roles: [...roles.entries()].map(([id, title]) => ({ id, title })),
    batches: [...batches].sort(),
    projects: [...projects].sort(),
  }
}

/** 侧栏项目列表：全部 + 已定义项目 + 作业里出现但未定义的项目，各带待处理数 */
export interface ProjectEntry { id: string; label: string; pending: number; defined: boolean }

export function projectEntries(jobs: JobSummary[], definitions: ProjectDef[]): ProjectEntry[] {
  const entries: ProjectEntry[] = [{ id: '', label: '全部项目', pending: 0, defined: true }]
  const byId = new Map<string, ProjectEntry>()
  const names = new Map<string, string>() // 项目名 → 定义 id
  for (const def of definitions) names.set(def.name, def.id)
  const ensure = (id: string, label: string, defined: boolean): ProjectEntry => {
    let e = byId.get(id)
    if (!e) {
      e = { id, label, pending: 0, defined }
      byId.set(id, e)
      entries.push(e)
    }
    return e
  }
  for (const def of definitions) ensure(def.id, def.name, true)
  for (const j of jobs) {
    const g = j.group || ''
    if (!g) continue
    const defId = names.get(g) ?? g
    const e = byId.get(defId) || ensure(defId, g, !byId.has(defId) && !names.has(g))
    if (needsAttention(j)) {
      e.pending += 1
      entries[0].pending += 1
    }
  }
  return entries
}
