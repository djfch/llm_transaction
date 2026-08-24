/** 交易 mock 必须与当前保证金下单协议和中文风控文案保持一致。 */
import { describe, expect, it } from 'vitest'
import { mockApi } from '../api/mock'

describe('交易 mock 协议', () => {
  it('所有 place_order 样例只使用保证金接口且风控原因不暴露内部键', async () => {
    const page = await mockApi.getRounds(0, 50)
    const calls = []
    for (const round of page.items) {
      const detail = await mockApi.getRound(round.round_id)
      calls.push(...detail.tool_calls.filter((call) => call.tool === 'place_order'))
    }

    expect(calls.length).toBeGreaterThan(0)
    for (const call of calls) {
      expect(call.args).not.toHaveProperty('size')
      expect(call.args).toMatchObject({
        side: expect.stringMatching(/^(long|short)$/),
        margin_usdt: expect.any(Number),
        leverage: expect.any(Number),
        stop_loss_price: expect.any(Number),
      })
      expect(call.risk_reason).not.toMatch(/max_position_pct|max_total_position_pct/)
    }
  })
})
