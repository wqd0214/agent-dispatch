// MCP / 技能登记页的纯逻辑：候选分组的展示数据、对照表的勾选计算（纯函数，可单测）

// 下发实现后删除这句话（连同各处引用）：见派工下发那张单
export const DISPATCH_PENDING_NOTE = '目前这里只做登记和配置，派工时的实际下发将在后续版本生效'

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

export interface CandidateGroup {
  name: string
  conflict: boolean
  variants: CandidateVariant[]
}

/** 按搜索词过滤候选分组（匹配名称 / 登记 ID / 来源 / 说明），过滤后没有变体的组不显示 */
export function filterCandidateGroups(groups: CandidateGroup[], query: string): CandidateGroup[] {
  const q = query.trim().toLowerCase()
  if (!q) return groups
  const hit = (v: CandidateVariant) =>
    `${v.name} ${v.id} ${v.source} ${v.path ?? ''} ${v.description ?? ''}`.toLowerCase().includes(q)
  return groups
    .map(g => ({ ...g, variants: g.variants.filter(hit) }))
    .filter(g => g.variants.length)
}

/** 全选用的：当前分组里所有未登记变体的登记 ID */
export function selectableCandidateIds(groups: CandidateGroup[]): string[] {
  return groups.flatMap(g => g.variants.filter(v => !v.registered).map(v => v.id))
}

/** MCP 条目一行摘要：local 给命令和参数，remote 给地址 */
export function mcpEntrySummary(entry: Record<string, unknown> | undefined): string {
  if (!entry) return ''
  if (entry.type === 'remote' || entry.url) return `remote · ${String(entry.url ?? '')}`
  const args = Array.isArray(entry.args) ? entry.args.join(' ') : ''
  return `local · ${[entry.command, args].filter(Boolean).join(' ')}`.trim()
}

/** 对照表：勾上 / 取消一个登记 ID 后的新列表（保持原有顺序，新勾的加在最后） */
export function toggleRegistryId(list: string[], id: string): string[] {
  return list.includes(id) ? list.filter(x => x !== id) : [...list, id]
}

/** 同一角色里选了两个 name 相同的条目时的提示（与服务端校验一致）；没有重复返回 null */
export function duplicateNameError(ids: string[], nameOf: (id: string) => string): string | null {
  const byName = new Map<string, string[]>()
  for (const id of ids) {
    const name = nameOf(id) || id
    byName.set(name, [...(byName.get(name) ?? []), id])
  }
  for (const [name, same] of byName) {
    if (same.length > 1) return `${name}（${same.join(' 与 ')}）：执行者按名字调用，一个角色只能留一个`
  }
  return null
}

/** used_by（[{project, role}]）转成「全局:后端、myproj:测试」一串文字；roleTitle 缺省用角色 ID */
export function usedByLabel(
  usedBy: { project: string; role: string }[],
  roleTitle: (project: string, role: string) => string,
): string {
  return usedBy.map(u => `${u.project || '全局'}:${roleTitle(u.project, u.role)}`).join('、')
}
