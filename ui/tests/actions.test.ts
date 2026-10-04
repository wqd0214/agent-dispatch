import { describe, expect, it } from 'vitest'
import { availableActions, canAttach, gateHint } from '../src/lib/actions'

describe('availableActions', () => {
  it('运行中只有叫停', () => {
    expect(availableActions({ state: 'running' })).toEqual([{ id: 'stop', label: '叫停', kind: 'danger' }])
  })
  it('待你决定走答复区，不给通用操作', () => {
    expect(availableActions({ state: 'running', needs_user: '问' })).toEqual([])
  })
  it('待审查 / 失败 / 中断 / 已叫停 / 已驳回给验收、返工、驳回', () => {
    for (const state of ['done', 'failed', 'interrupted', 'stopped', 'rejected']) {
      expect(availableActions({ state }).map(a => a.id)).toEqual(['accept', 'rework', 'reject'])
    }
  })
  it('人工持有与已验收没有操作', () => {
    expect(availableActions({ state: 'held' })).toEqual([])
    expect(availableActions({ state: 'accepted' })).toEqual([])
  })
})

describe('canAttach', () => {
  it('tmux 且界面开着才显示', () => {
    expect(canAttach({ mode: 'tmux', tmux_alive: true })).toBe(true)
    expect(canAttach({ mode: 'tmux', tmux_alive: false })).toBe(false)
    expect(canAttach({ mode: 'headless', tmux_alive: true })).toBe(false)
  })
})

describe('gateHint', () => {
  it('没跑和没过都提示，过了不提示', () => {
    expect(gateHint('none')).toContain('先跑闸门')
    expect(gateHint('fail')).toContain('先跑闸门')
    expect(gateHint('ok')).toBe('')
  })
})
