// MCP / 技能登记页的纯逻辑：候选分组的展示数据、对照表的勾选计算（纯函数，可单测）

// MCP / 技能页顶部的说明：下发已实现，写明实际行为与两条局限
export const DISPATCH_PENDING_NOTE = '角色「开始管理」后，派工时只带勾选的下发。两条局限：pi 在管理 MCP 时不加载 ~/.pi/agent/settings.json 里的扩展包；OpenCode 无法排除全局技能，只能追加。'

export interface CandidateVariant {
  id: string
  name: string
  registered: boolean
  source: string
  sources?: string[]
  project?: string
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

/** 按项目视图合并后的主表行：已登记的（本项目 + 全局）与发现未纳入的候选在一棵表里 */
export interface RegistryRow {
  key: string
  id: string
  name: string
  kind: 'entry' | 'candidate'
  status: 'enabled' | 'included' | 'unmanaged' | 'discovered'
  owner: string // '' = 全局，否则项目 ID
  entry?: Record<string, unknown>
  path?: string
  description?: string
  sources: string[]
  diff?: string
  variant?: CandidateVariant
}

export const STATUS_LABEL: Record<RegistryRow['status'], string> = {
  enabled: '已启用', included: '已纳入未启用', unmanaged: '未管理', discovered: '发现未纳入',
}

/** 把登记条目与候选变体合并成主表行：scope 是当前项目 ID（'' = 全局视图）。
 * 已登记条目的状态看本项目是否管理、是否在启用清单里；候选只显示归属同一范围的未登记变体。 */
export function mergeRegistryRows(
  entries: { id: string; name: string; project?: string; sources?: string[]; description?: string; path?: string; [k: string]: unknown }[],
  candidates: CandidateGroup[],
  opts: { managed: boolean; enabled: string[]; scope: string },
): RegistryRow[] {
  const rows: RegistryRow[] = entries.map(e => ({
    key: e.id, id: e.id, name: e.name, kind: 'entry',
    status: !opts.managed ? 'unmanaged' : (opts.enabled.includes(e.id) ? 'enabled' : 'included'),
    owner: e.project ?? '', entry: e as Record<string, unknown>, path: e.path,
    description: e.description ?? '', sources: e.sources ?? [],
  }))
  for (const g of candidates) {
    for (const v of g.variants) {
      if (v.registered || (v.project ?? '') !== opts.scope) continue
      rows.push({
        key: `cand:${v.id}`, id: v.id, name: v.name, kind: 'candidate', status: 'discovered',
        owner: v.project ?? '', entry: v.entry, path: v.path, description: v.description ?? '',
        sources: v.sources ?? [], variant: v,
        diff: g.conflict
          ? [mcpEntrySummary(v.entry), ...Object.entries(v.diff ?? {}).map(([k, x]) => `${k}=${JSON.stringify(x)}`)].filter(Boolean).join(' ')
          : '',
      })
    }
  }
  return rows
}

/** 「全部纳入」要导入的候选登记 ID（当前范围内未登记的变体） */
export function discoveredCandidateIds(candidates: CandidateGroup[], scope: string): string[] {
  return candidates.flatMap(g => g.variants.filter(v => !v.registered && (v.project ?? '') === scope).map(v => v.id))
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
