import { describe, expect, it } from 'vitest'
import { EMPTY_FILTERS, filterJobs, needsAttention, projectEntries, queueText, sortJobs, stateTag, statusTag } from '../src/lib/jobs'
import type { JobSummary, ProjectDef } from '../src/types'

function job(partial: Partial<JobSummary>): JobSummary {
  return {
    name: 'j', batch: '', profile: '', group: 'G', project: 'P', role: 'dev', role_title: '开发',
    backend: 'pi', model: 'm', model_alias: null, model_actual: null, state: 'done', state_label: '待审查',
    status: null, turn: 1, seconds: 10, channels: [], task: '', needs_user: null, reason: null,
    gate_ok: null, created: '', mode: 'headless', updated: '', ended: null, activity: '',
    ...partial,
  }
}

describe('filterJobs', () => {
  const jobs = [
    job({ name: 'a', state: 'running', role: 'dev', batch: 'b1', project: 'backend', task: '修 BUG' }),
    job({ name: 'b', state: 'done', role: 'tester', batch: 'b2', project: 'test', task: '跑场景' }),
    job({ name: 'c', state: 'accepted', role: 'dev', project: 'backend' }),
  ]
  it('无筛选条件时全过', () => {
    expect(filterJobs(jobs, { ...EMPTY_FILTERS })).toHaveLength(3)
  })
  it('状态多选、角色、批次、工程分别生效', () => {
    expect(filterJobs(jobs, { ...EMPTY_FILTERS, states: ['running', 'done'] }).map(j => j.name)).toEqual(['a', 'b'])
    expect(filterJobs(jobs, { ...EMPTY_FILTERS, role: 'dev' }).map(j => j.name)).toEqual(['a', 'c'])
    expect(filterJobs(jobs, { ...EMPTY_FILTERS, batch: 'b2' }).map(j => j.name)).toEqual(['b'])
    expect(filterJobs(jobs, { ...EMPTY_FILTERS, project: 'backend' }).map(j => j.name)).toEqual(['a', 'c'])
  })
  it('搜索匹配作业名和任务文本（不区分大小写）', () => {
    expect(filterJobs(jobs, { ...EMPTY_FILTERS, search: 'bug' }).map(j => j.name)).toEqual(['a'])
    expect(filterJobs(jobs, { ...EMPTY_FILTERS, search: 'C' }).map(j => j.name)).toEqual(['c'])
    expect(filterJobs(jobs, { ...EMPTY_FILTERS, search: '不存在' })).toHaveLength(0)
  })
})

describe('sortJobs', () => {
  it('按最近活动（updated）倒序', () => {
    const sorted = sortJobs([
      job({ name: 'old', updated: '2026-10-01 10:00:00' }),
      job({ name: 'new', updated: '2026-10-04 09:00:00' }),
      job({ name: 'mid', updated: '2026-10-02 10:00:00' }),
    ])
    expect(sorted.map(j => j.name)).toEqual(['new', 'mid', 'old'])
  })
  it('不改原数组', () => {
    const input = [job({ updated: '1' }), job({ updated: '2' })]
    sortJobs(input)
    expect(input[0].updated).toBe('1')
  })
})

describe('stateTag / statusTag / needsAttention', () => {
  it('待你决定优先于底层状态', () => {
    expect(stateTag(job({ state: 'running', needs_user: '要不要清库？' }))).toEqual({ label: '待你决定', type: 'warning' })
  })
  it('状态映射', () => {
    expect(stateTag(job({ state: 'running' })).type).toBe('primary')
    expect(stateTag(job({ state: 'failed' })).type).toBe('danger')
    expect(stateTag(job({ state: 'accepted' })).type).toBe('success')
    expect(stateTag(job({ state: 'weird', state_label: '怪状态' })).label).toBe('怪状态')
  })
  it('STATUS 映射与空值', () => {
    expect(statusTag('PASS')!.type).toBe('success')
    expect(statusTag('NEEDS_DECISION')!.type).toBe('warning')
    expect(statusTag(null)).toBeNull()
  })
  it('待处理判定', () => {
    expect(needsAttention(job({ state: 'done' }))).toBe(true)
    expect(needsAttention(job({ state: 'failed' }))).toBe(true)
    expect(needsAttention(job({ state: 'running', needs_user: '问' }))).toBe(true)
    expect(needsAttention(job({ state: 'running' }))).toBe(false)
    expect(needsAttention(job({ state: 'accepted' }))).toBe(false)
  })
})

describe('projectEntries', () => {
  const defs: ProjectDef[] = [
    { id: 'c2', name: 'WMS_ZC_C2', roles: [], repos: [] },
    { id: 'a04', name: 'WMS_ZC_A04', roles: [], repos: [] },
  ]
  it('全部项目 + 已定义项目 + 未定义项目，带待处理角标', () => {
    const jobs = [
      job({ group: 'WMS_ZC_C2', state: 'done' }),
      job({ group: 'WMS_ZC_C2', state: 'accepted' }),
      job({ group: 'WMS_ZC_A04', state: 'running', needs_user: '问' }),
      job({ group: '别的项目', state: 'failed' }),
    ]
    const entries = projectEntries(jobs, defs)
    expect(entries.map(e => e.label)).toEqual(['全部项目', 'WMS_ZC_C2', 'WMS_ZC_A04', '别的项目'])
    expect(entries.map(e => e.pending)).toEqual([3, 1, 1, 1])
  })
})

describe('queueText（排队中 · 等 <模型>）', () => {
  it('排队中的作业列出在等的模型', () => {
    expect(queueText(job({
      state: 'queued', state_label: '排队中',
      waiting_models: [{ backend: 'pi', id: 'p/m', alias: 'm' }, { backend: 'opencode', id: 'o/m', alias: null }],
    }))).toBe('排队中 · 等 pi:m、opencode:o/m')
  })
  it('没有在等清单时只显示「排队中」；非排队返回 null', () => {
    expect(queueText(job({ state: 'queued', state_label: '排队中' }))).toBe('排队中')
    expect(queueText(job({ state: 'running' }))).toBeNull()
  })
  it('stateTag 对排队作业给出排队文案', () => {
    const tag = stateTag(job({ state: 'queued', state_label: '排队中', waiting_models: [{ backend: 'pi', id: 'p/m', alias: 'm' }] }))
    expect(tag.label).toBe('排队中 · 等 pi:m')
    expect(tag.type).toBe('info')
  })
})
