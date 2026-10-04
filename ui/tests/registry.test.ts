import { describe, expect, it } from 'vitest'
import {
  DISPATCH_PENDING_NOTE, duplicateNameError, filterCandidateGroups, mcpEntrySummary,
  selectableCandidateIds, toggleRegistryId, usedByLabel,
} from '../src/lib/registry'

const groups = [
  { name: 'playwright', conflict: true, variants: [
    { id: 'playwright@a', name: 'playwright', registered: false, source: 'pi 全局', diff: { command: 'npx-a' } },
    { id: 'playwright@b', name: 'playwright', registered: true, source: 'OpenCode 全局', diff: { command: 'npx-b' } },
  ] },
  { name: 'fetch', conflict: false, variants: [
    { id: 'fetch', name: 'fetch', registered: false, source: 'pi 全局', description: '抓网页' },
  ] },
]

describe('filterCandidateGroups', () => {
  it('无搜索词原样返回', () => {
    expect(filterCandidateGroups(groups, '')).toBe(groups)
    expect(filterCandidateGroups(groups, '  ')).toBe(groups)
  })
  it('按名称 / 登记 ID / 来源 / 说明过滤，空组丢弃', () => {
    expect(filterCandidateGroups(groups, 'playwright')).toEqual([groups[0]])
    expect(filterCandidateGroups(groups, '@b')).toEqual([{ ...groups[0], variants: [groups[0].variants[1]] }])
    expect(filterCandidateGroups(groups, '抓网页')).toEqual([groups[1]])
    expect(filterCandidateGroups(groups, '不存在')).toEqual([])
  })
})

describe('selectableCandidateIds', () => {
  it('只给未登记的变体 ID', () => {
    expect(selectableCandidateIds(groups)).toEqual(['playwright@a', 'fetch'])
  })
})

describe('mcpEntrySummary', () => {
  it('local 给命令加参数，remote 给地址', () => {
    expect(mcpEntrySummary({ type: 'local', command: 'npx', args: ['-y', 'pkg'] })).toBe('local · npx -y pkg')
    expect(mcpEntrySummary({ type: 'remote', url: 'https://x/sse' })).toBe('remote · https://x/sse')
    expect(mcpEntrySummary(undefined)).toBe('')
  })
})

describe('toggleRegistryId / duplicateNameError', () => {
  it('勾上追加到最后，取消原地去掉', () => {
    expect(toggleRegistryId(['a'], 'b')).toEqual(['a', 'b'])
    expect(toggleRegistryId(['a', 'b'], 'a')).toEqual(['b'])
  })
  it('同名重复给出与服务端一致的提示；不重复返回 null', () => {
    const nameOf = (id: string) => (id === 'x@1' || id === 'x@2' ? 'x' : id)
    expect(duplicateNameError(['x@1', 'x@2'], nameOf)).toBe('x（x@1 与 x@2）：执行者按名字调用，一个角色只能留一个')
    expect(duplicateNameError(['x@1', 'y'], nameOf)).toBeNull()
  })
})

describe('usedByLabel', () => {
  it('全局角色不带项目名，其它带项目 ID；称呼缺省用角色 ID', () => {
    expect(usedByLabel([{ project: '', role: 'dev' }, { project: 'p1', role: 'qa' }], (p, r) => (r === 'dev' ? '开发' : r)))
      .toBe('全局:开发、p1:qa')
  })
})

it('顶部说明是一个可删的常量', () => {
  expect(DISPATCH_PENDING_NOTE).toContain('后续版本生效')
})
