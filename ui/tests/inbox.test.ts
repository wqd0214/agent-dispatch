import { describe, expect, it } from 'vitest'
import { gateState, inboxGroupOf, inboxGroups } from '../src/lib/inbox'
import type { JobSummary } from '../src/types'

function job(partial: Partial<JobSummary>): JobSummary {
  return {
    name: 'j', batch: '', profile: '', group: 'G', project: 'P', role: 'dev', role_title: '开发',
    backend: 'pi', model: 'm', model_alias: null, model_actual: null, state: 'done', state_label: '待审查',
    status: null, turn: 1, seconds: 10, channels: [], task: '', needs_user: null, reason: null,
    gate_ok: null, created: '', mode: 'headless', updated: '', ended: null, activity: '',
    ...partial,
  }
}

describe('inboxGroupOf', () => {
  it('待你决定优先于底层状态', () => {
    expect(inboxGroupOf(job({ state: 'running', needs_user: '要不要清库？' }))).toBe('decide')
    expect(inboxGroupOf(job({ state: 'done', needs_user: '问' }))).toBe('decide')
  })
  it('四组各自的状态映射', () => {
    expect(inboxGroupOf(job({ state: 'done' }))).toBe('review')
    expect(inboxGroupOf(job({ state: 'failed' }))).toBe('broken')
    expect(inboxGroupOf(job({ state: 'interrupted' }))).toBe('broken')
    expect(inboxGroupOf(job({ state: 'running' }))).toBe('running')
  })
  it('已验收 / 已驳回 / 已叫停 / 人工持有不进工作台', () => {
    for (const state of ['accepted', 'rejected', 'stopped', 'held', 'weird']) {
      expect(inboxGroupOf(job({ state }))).toBeNull()
    }
  })
})

describe('inboxGroups', () => {
  it('四组固定顺序，组内按 updated 倒序', () => {
    const groups = inboxGroups([
      job({ name: 'run-a', state: 'running' }),
      job({ name: 'decide-a', needs_user: '问' }),
      job({ name: 'fail-a', state: 'failed' }),
      job({ name: 'review-new', state: 'done', updated: '2026-10-04 10:00:00' }),
      job({ name: 'review-old', state: 'done', updated: '2026-10-01 10:00:00' }),
      job({ name: 'ok', state: 'accepted' }),
    ])
    expect(groups.map(g => g.key)).toEqual(['running', 'decide', 'review', 'broken'])
    expect(groups.map(g => g.jobs.length)).toEqual([1, 1, 2, 1])
    expect(groups[2].jobs.map(j => j.name)).toEqual(['review-new', 'review-old'])
    expect(groups.map(g => g.title)).toEqual(['正在执行', '等你决定', '待审查', '失败与中断'])
  })
  it('没有正在执行的单时该组仍在（长度为 0），供界面显示占位行', () => {
    const groups = inboxGroups([job({ state: 'done' })])
    expect(groups[0].key).toBe('running')
    expect(groups[0].jobs).toEqual([])
  })
  it('全空时四组都空', () => {
    expect(inboxGroups([]).every(g => g.jobs.length === 0)).toBe(true)
  })
})

describe('gateState', () => {
  it('true=ok，false=fail，空=none（未跑）', () => {
    expect(gateState({ gate_ok: true })).toBe('ok')
    expect(gateState({ gate_ok: false })).toBe('fail')
    expect(gateState({ gate_ok: null })).toBe('none')
  })
})
