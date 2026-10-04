// 角色页的纯逻辑：一句话现状、模型池比例、层级判断（纯函数，可单测）

import type { PoolEntry, RoleDetail } from '../types'

export interface PoolRow { model: string; weight: number; pct: number }

/** 模型池各条折算后的百分比；总比例为 0 时全部记 0 */
export function poolRows(pool: PoolEntry[] | null | undefined): PoolRow[] {
  const entries = (pool || []).map(e => ({ model: e.model, weight: Number(e.weight) || 0 }))
  const total = entries.reduce((n, e) => n + e.weight, 0)
  return entries.map(e => ({ ...e, pct: total ? Math.round((e.weight * 100) / total) : 0 }))
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
    const rows = poolRows(pool).map(r => `${r.model} ${r.pct}%`).join('、')
    parts.push(`派工时用 ${backend || '默认工具'}，在 ${rows} 之间按比例选`)
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
