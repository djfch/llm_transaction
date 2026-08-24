/** 交易 mock 必须与当前保证金下单协议和中文风控文案保持一致。 */
import { describe, expect, it } from 'vitest'
import { mockApi } from '../api/mock'

describe('交易 mock 协议', () => {
  it('开仓使用保证金协议、平仓使用 close 协议且风控原因不暴露内部键', async () => {
    const page = await mockApi.getRounds(0, 50)
    const calls = []
    for (const round of page.items) {
      const detail = await mockApi.getRound(round.round_id)
      calls.push(...detail.tool_calls.filter((call) => call.tool === 'place_order'))
    }

    type StructuredCall = (typeof calls)[number] & { args: Record<string, unknown> }
    const structuredCalls = calls.filter(
      (call): call is StructuredCall => typeof call.args !== 'string',
    )
    const exposureCalls = structuredCalls.filter((call) => call.args.close !== true)
    const closeCalls = structuredCalls.filter((call) => call.args.close === true)
    expect(exposureCalls.length).toBeGreaterThan(0)
    expect(closeCalls.length).toBeGreaterThan(0)
    for (const call of exposureCalls) {
      expect(call.args).not.toHaveProperty('size')
      expect(call.args).toMatchObject({
        side: expect.stringMatching(/^(long|short)$/),
        margin_usdt: expect.any(Number),
        leverage: expect.any(Number),
        stop_loss_price: expect.any(Number),
      })
      expect(call.risk_reason).not.toMatch(/max_position_pct|max_total_position_pct/)
    }
    for (const call of closeCalls) {
      expect(call.args).toEqual({ contract: expect.any(String), close: true })
      expect(call.risk_reason).not.toMatch(/max_position_pct|max_total_position_pct/)
    }
  })
})
