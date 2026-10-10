// 角色页的纯逻辑：一句话现状、模型池优先级与比例、层级判断（纯函数，可单测）

import type { ConfigOverview, PoolEntry, RoleDetail } from '../types'

export interface PoolRow { model: string; weight: number; priority: number; pct: number }

/** 模型池各条的优先级（缺省 1）和在同一优先级里折算后的百分比；同档总比例为 0 时记 0 */
export function poolRows(pool: PoolEntry[] | null | undefined): PoolRow[] {
  const entries = (pool || []).map(e => ({ model: e.model, weight: Number(e.weight) || 0, priority: Number(e.priority) || 1 }))
  const totals = new Map<number, number>()
  for (const e of entries) totals.set(e.priority, (totals.get(e.priority) || 0) + e.weight)
  return entries.map(e => {
    const total = totals.get(e.priority) || 0
    return { ...e, pct: total ? Math.round((e.weight * 100) / total) : 0 }
  })
}

/** 模型池按优先级分档，高优先级（数字小）在前 */
export function poolTiers(pool: PoolEntry[] | null | undefined): PoolRow[][] {
  const tiers = new Map<number, PoolRow[]>()
  for (const row of poolRows(pool)) tiers.set(row.priority, [...(tiers.get(row.priority) || []), row])
  return [...tiers.entries()].sort((a, b) => a[0] - b[0]).map(([, rows]) => rows)
}

function field<T>(detail: RoleDetail, key: string, fallback: T): T {
  const f = detail.fields[key]
  return f === undefined ? fallback : (f.value as T)
}

/** 一句话现状：派工工具、模型（池或固定）、工程、运行方式、单轮超时 */
export function summarizeRole(detail: RoleDetail, repoName: (id: string) => string): string {
  const parts: string[] = []
  const backend = field<string>(detail, 'backend', '')
  const pool = field<PoolEntry[] | null>(detail, 'model_pool', null)
  if (pool && pool.length) {
    const tiers = poolTiers(pool).map(rows => rows.map(r => `${r.model} ${r.pct}%`).join('、'))
    const tool = `派工时用 ${backend || '默认工具'}`
    parts.push(tiers.length > 1
      ? `${tool}，优先在 ${tiers[0]} 之间按比例选，都不可用时依次换 ${tiers.slice(1).join(' → ')}`
      : `${tool}，在 ${tiers[0]} 之间按比例选`)
  } else {
    const model = field<string>(detail, 'model', '')
    const modelsMap = field<Record<string, string> | null>(detail, 'models', null)
    const mapped = backend && modelsMap ? modelsMap[backend] : ''
    const chosen = model || mapped
    parts.push(`派工时用 ${backend || '默认工具'}${chosen ? `，固定模型 ${chosen}` : '，模型由工具决定'}`)
  }
  if (detail.repos.length) parts.push(`在 ${detail.repos.map(repoName).join('、')} 干活`)
  const mode = field<string>(detail, 'mode', 'headless')
  parts.push(`${mode} 方式`)
  const timeout = field<number | null>(detail, 'timeout_min', null)
  if (timeout) parts.push(`单轮超时 ${timeout} 分钟`)
  return parts.join('；')
}

/** 这一角色在「本机全局 / 项目」层有没有自己的定义（能不能删、能不能恢复继承） */
export function hasOwnLayer(detail: RoleDetail): boolean {
  const own = ['本机全局', '项目']
  if (detail.brief.source && own.includes(detail.brief.source)) return true
  return Object.values(detail.fields).some(f => own.includes(f.source))
}

/** 某个字段在项目 / 本机全局层覆盖过（显示「恢复继承」用） */
export function fieldOverridden(detail: RoleDetail, key: string): boolean {
  const own = ['本机全局', '项目']
  const f = detail.fields[key]
  return !!f && own.includes(f.source)
}

/** 项目视图下角色列表每一项的继承状态标签；全部项目视图返回空串（不显示） */
export function inheritLabel(scope: string, inProjectView: boolean): string {
  if (!inProjectView) return ''
  if (scope === '全局') return '继承全局'
  if (scope === '项目覆盖全局') return '本项目已单独配置'
  return '项目专属'
}

export interface RoleListRow { id: string; title: string; description: string; scope: string }

/** 某个项目视图下要列出的角色：已定义项目给合并后的角色（含继承的全局角色，带 scope）；
 * 没定义过的项目照样给全局角色（全部标「继承全局」），页面才不至于空掉 */
export function projectRoleRows(config: ConfigOverview | null, projectId: string): { rows: RoleListRow[]; defined: boolean } {
  if (!config) return { rows: [], defined: true }
  if (!projectId) return { rows: config.roles.map(r => ({ ...r, scope: '全局' })), defined: true }
  const project = config.projects.find(p => p.id === projectId)
  if (project) return { rows: project.roles.map(r => ({ ...r, scope: r.scope || '项目专属' })), defined: true }
  return { rows: config.roles.map(r => ({ ...r, scope: '全局' })), defined: false }
}

/** 定义项目对话框的工程预填：该项目已有作业按工程名去重，每个工程取一个代表作业名（用于查工作目录） */
export function uniqueRepoSamples(jobs: { project: string; name: string }[]): { name: string; sample: string }[] {
  const byName = new Map<string, string>()
  for (const j of jobs) {
    if (j.project && !byName.has(j.project)) byName.set(j.project, j.name)
  }
  return [...byName.entries()].map(([name, sample]) => ({ name, sample }))
}
