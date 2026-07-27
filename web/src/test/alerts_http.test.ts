import { afterEach, describe, expect, it, vi } from 'vitest'
import { httpApi } from '../api/http'

afterEach(() => vi.unstubAllGlobals())

describe('alerts HTTP 适配', () => {
  it('将后端 Decimal 字符串 price 转为数字，created_at(Unix秒) 转 ISO time', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(
        async () =>
          new Response(
            JSON.stringify([
              {
                id: 1,
                round_id: 'r1',
                contract: 'BTC_USDT',
                direction: 'above',
                price: '52000',
                active: true,
                created_at: 1_700_000_000,
              },
            ]),
            { status: 200 },
          ),
      ),
    )

    const [alert] = await httpApi.getAlerts()
    expect(alert).toEqual({
      id: 1,
      contract: 'BTC_USDT',
      direction: 'above',
      price: 52000,
      time: new Date(1_700_000_000 * 1000).toISOString(),
    })
  })
})
