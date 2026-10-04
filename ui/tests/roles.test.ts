import { describe, expect, it } from 'vitest'
import { fieldOverridden, hasOwnLayer, poolRows, summarizeRole } from '../src/lib/roles'
import type { RoleDetail } from '../src/types'

function detail(fields: Record<string, { value: unknown; source: string }>, extra: Partial<RoleDetail> = {}): RoleDetail {
  return {
    role: 'dev', title: '开发', scope: '项目覆盖全局', repos: ['backend'], pool_source: null,
    fields, brief: { text: null, source: '随工具发布', path: null }, ...extra,
  }
}

describe('poolRows', () => {
  it('按比例折算百分比', () => {
    expect(poolRows([{ model: 'a', weight: 1 }, { model: 'b', weight: 1 }])).toEqual([
      { model: 'a', weight: 1, pct: 50 }, { model: 'b', weight: 1, pct: 50 },
    ])
    expect(poolRows([{ model: 'a', weight: 3 }, { model: 'b', weight: 1 }])[0].pct).toBe(75)
  })
  it('空池与总比例为 0 时不炸', () => {
    expect(poolRows(null)).toEqual([])
    expect(poolRows([{ model: 'a', weight: 0 }])).toEqual([{ model: 'a', weight: 0, pct: 0 }])
  })
})

describe('summarizeRole', () => {
  const name = (id: string) => (id === 'backend' ? '后端工程' : id)
  it('模型池：工具 + 池内比例 + 工程 + 方式 + 超时', () => {
    const text = summarizeRole(detail({
      backend: { value: 'pi', source: '随工具发布' },
      model_pool: { value: [{ model: 'glm-a', weight: 1 }, { model: 'glm-b', weight: 1 }], source: '项目' },
      mode: { value: 'tmux', source: '随工具发布' },
      timeout_min: { value: 180, source: '项目' },
    }), name)
    expect(text).toBe('派工时用 pi，在 glm-a 50%、glm-b 50% 之间按比例选；在 后端工程 干活；tmux 方式；单轮超时 180 分钟')
  })
  it('没有模型池时展示固定模型，缺的字段跳过', () => {
    const text = summarizeRole(detail({
      backend: { value: 'opencode', source: '随工具发布' },
      model: { value: 'gpt-x', source: '本机全局' },
    }), name)
    expect(text).toBe('派工时用 opencode，固定模型 gpt-x；在 后端工程 干活；headless 方式')
  })
})

describe('层级判断', () => {
  it('只有随工具发布的字段时既不能删也不能恢复', () => {
    const d = detail({ backend: { value: 'pi', source: '随工具发布' } })
    expect(hasOwnLayer(d)).toBe(false)
    expect(fieldOverridden(d, 'backend')).toBe(false)
  })
  it('本机全局 / 项目覆盖过的字段可以恢复，角色可以删', () => {
    const d = detail({
      backend: { value: 'pi', source: '随工具发布' },
      timeout_min: { value: 20, source: '项目' },
    })
    expect(hasOwnLayer(d)).toBe(true)
    expect(fieldOverridden(d, 'timeout_min')).toBe(true)
    expect(fieldOverridden(d, 'backend')).toBe(false)
  })
})
