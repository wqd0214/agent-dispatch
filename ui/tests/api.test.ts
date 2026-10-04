import { describe, expect, it } from 'vitest'
import { errText } from '../src/api'

function jsonRes(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status })
}

describe('errText 的 404 判定', () => {
  it('404 且 error 是 not found 时提示服务没重启到新版本', async () => {
    expect(await errText(jsonRes(404, { error: 'not found' })))
      .toBe('网页服务不认识这个接口，很可能是服务还没重启到新版本')
  })
  it('其它 404 错误原样显示，不误判', async () => {
    expect(await errText(jsonRes(404, { error: '没有这个批次：x' }))).toBe('没有这个批次：x')
  })
  it('非 JSON 响应退回通用文案', async () => {
    expect(await errText(new Response('oops', { status: 500 }))).toBe('请求失败（500）')
  })
  it('带 message 的响应优先用 message', async () => {
    expect(await errText(jsonRes(400, { ok: false, message: '内容为空' }))).toBe('内容为空')
  })
})
