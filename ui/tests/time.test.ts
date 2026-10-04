import { describe, expect, it } from 'vitest'
import { formatDuration, relativeTime } from '../src/lib/time'

const NOW = Date.parse('2026-10-04T12:00:00')

describe('relativeTime', () => {
  it('把本地时间串变成相对时间', () => {
    expect(relativeTime('2026-10-04 11:48:30', NOW)).toBe('11 分钟前')
    expect(relativeTime('2026-10-04 11:00:00', NOW)).toBe('1 小时前')
    expect(relativeTime('2026-10-04 12:00:00', NOW)).toBe('刚刚')
  })
  it('超过一个月回退为原始时间串', () => {
    expect(relativeTime('2026-09-01 10:00:00', NOW)).toBe('2026-09-01 10:00:00')
  })
  it('空值与坏值', () => {
    expect(relativeTime(null)).toBe('-')
    expect(relativeTime('')).toBe('-')
    expect(relativeTime('乱写')).toBe('乱写')
  })
})

describe('formatDuration', () => {
  it('常见耗时', () => {
    expect(formatDuration(45)).toBe('45 秒')
    expect(formatDuration(451)).toBe('7 分 31 秒')
    expect(formatDuration(600)).toBe('10 分')
    expect(formatDuration(27_000)).toBe('7 小时 30 分')
    expect(formatDuration(7200)).toBe('2 小时')
  })
  it('空值给 -', () => {
    expect(formatDuration(null)).toBe('-')
    expect(formatDuration(undefined)).toBe('-')
  })
})
