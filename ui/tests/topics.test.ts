import { describe, expect, it } from 'vitest'
import { busyChannels, followsTree, topicCounts, topicGroups } from '../src/lib/topics'
import type { JobSummary } from '../src/types'

function job(partial: Partial<JobSummary>): JobSummary {
  return {
    name: 'j', batch: '', batch_title: '', follows: [], profile: '', group: 'G', project: 'P',
    role: 'dev', role_title: '开发', backend: 'pi', model: 'm', model_alias: null, model_actual: null,
    state: 'done', state_label: '待审查', status: null, turn: 1, seconds: 10, channels: [], task: '',
    needs_user: null, reason: null, gate_ok: null, created: '', mode: 'headless', updated: '',
    ended: null, activity: '', dispatcher: null,
    ...partial,
  }
}

describe('topicGroups', () => {
  it('只列出还有活干的事项，有正在执行的排前面', () => {
    const groups = topicGroups([
      job({ name: 'a1', batch: 'A', state: 'done', updated: '2026-10-01 10:00:00' }),
      job({ name: 'b1', batch: 'B', state: 'running' }),
      job({ name: 'a2', batch: 'A', state: 'accepted' }),
      job({ name: 'a3', batch: 'A', state: 'failed' }),
      job({ name: 'c1', batch: 'C', state: 'accepted' }), // 全部落定的事项不出现
    ])
    expect(groups.map(g => g.batch)).toEqual(['B', 'A'])
    expect(groups[1].pending.map(j => j.name)).toEqual(['a1', 'a3'])
    expect(groups[1].doneCount).toBe(1)
  })

  it('组头信息：标题、派工者取第一张有登记的单', () => {
    const groups = topicGroups([
      job({ name: 'x1', batch: 'X', batch_title: '修复超时', state: 'running',
            dispatcher: { kind: 'pi', session_id: 's1', title: '主管会话' } }),
    ])
    expect(groups[0].title).toBe('修复超时')
    expect(groups[0].dispatcher?.kind).toBe('pi')
  })

  it('没有批次名的单归到同一组（batch 为空串）', () => {
    const groups = topicGroups([job({ name: 'n1', state: 'running' })])
    expect(groups).toHaveLength(1)
    expect(groups[0].batch).toBe('')
    expect(groups[0].title).toBe('')
  })
})

describe('followsTree', () => {
  it('follows 的单子缩进在跟随对象下面，其余按派出顺序', () => {
    const nodes = followsTree([
      job({ name: 'fix-r2', created: '2026-10-03 10:00:00', follows: ['fix-r1'] }),
      job({ name: 'base', created: '2026-10-01 10:00:00' }),
      job({ name: 'fix-r1', created: '2026-10-02 10:00:00', follows: ['base'] }),
      job({ name: 'other', created: '2026-10-02 11:00:00' }),
    ])
    expect(nodes.map(n => [n.job.name, n.depth])).toEqual([
      ['base', 0], ['fix-r1', 1], ['fix-r2', 2], ['other', 0],
    ])
  })

  it('跟随对象不在列表里时留在顶层，仍带 follows 标注', () => {
    const nodes = followsTree([job({ name: 'a', follows: ['gone'] })])
    expect(nodes[0].depth).toBe(0)
    expect(nodes[0].follows).toEqual(['gone'])
  })
})

describe('topicCounts', () => {
  it('与后端口径一致', () => {
    expect(topicCounts([
      job({ state: 'running' }), job({ state: 'held' }), job({ state: 'done' }),
      job({ state: 'accepted' }), job({ state: 'rejected' }), job({ state: 'stopped' }),
      job({ state: 'failed' }), job({ state: 'interrupted' }),
    ])).toEqual({ running: 2, review: 1, done: 3, failed: 2 })
  })
})

describe('busyChannels', () => {
  it('只挑出被占用的通道', () => {
    const busy = busyChannels([
      { project: 'web', table: { c1: 'job-a', c2: null } },
      { project: 'app', table: { c1: 'job-b' } },
    ])
    expect(busy).toEqual([
      { project: 'web', channel: 'c1', holder: 'job-a' },
      { project: 'app', channel: 'c1', holder: 'job-b' },
    ])
  })
})
