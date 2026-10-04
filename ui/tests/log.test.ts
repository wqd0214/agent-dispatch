import { describe, expect, it } from 'vitest'
import { appendLog } from '../src/lib/log'

describe('appendLog', () => {
  it('offset 连续时拼接', () => {
    let s = { offset: 0, text: '' }
    s = appendLog(s, { offset: 5, text: 'hello' })
    expect(s).toEqual({ offset: 5, text: 'hello' })
    s = appendLog(s, { offset: 11, text: ' world' })
    expect(s).toEqual({ offset: 11, text: 'hello world' })
  })
  it('offset 回退或跳变时用新内容整体替换，避免重复', () => {
    const s = { offset: 100, text: '旧内容' }
    expect(appendLog(s, { offset: 20, text: '重来' })).toEqual({ offset: 20, text: '重来' })
    expect(appendLog(s, { offset: 300, text: '跳变' })).toEqual({ offset: 300, text: '跳变' })
  })
})
