import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { debounce } from '../src/lib/debounce'

beforeEach(() => vi.useFakeTimers())
afterEach(() => vi.useRealTimers())

describe('debounce', () => {
  it('停顿 500 毫秒后才执行一次', () => {
    const fn = vi.fn()
    const d = debounce(fn, 500)
    d('a')
    vi.advanceTimersByTime(300)
    d('b')
    expect(fn).not.toHaveBeenCalled()
    vi.advanceTimersByTime(500)
    expect(fn).toHaveBeenCalledTimes(1)
    expect(fn).toHaveBeenCalledWith('b')
  })
  it('连续输入只保留最后一次的参数', () => {
    const fn = vi.fn()
    const d = debounce(fn, 500)
    for (const v of ['x', 'y', 'z']) d(v)
    vi.advanceTimersByTime(500)
    expect(fn.mock.calls).toEqual([['z']])
  })
  it('间隔超过 500 毫秒的两次输入各执行一次', () => {
    const fn = vi.fn()
    const d = debounce(fn, 500)
    d(1)
    vi.advanceTimersByTime(500)
    d(2)
    vi.advanceTimersByTime(500)
    expect(fn.mock.calls).toEqual([[1], [2]])
  })
})
