import { afterEach, describe, expect, it, vi } from 'vitest'
import { httpApi } from '../api/http'

afterEach(() => vi.unstubAllGlobals())

describe('open_orders HTTP 适配', () => {
  it('将后端 Decimal 字符串转为数字', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response(
          JSON.stringify([
            {
              id: 'order-1',
              contract: 'ETH_USDT',
              size: '-79',
              left: '40',
              price: '1900',
              tif: 'gtc',
              reduce_only: true,
              status: 'open',
            },
          ]),
          { status: 200 },
        ),
      ),
    )

    const [order] = await httpApi.getOpenOrders()
    expect(order).toEqual({
      id: 'order-1',
      contract: 'ETH_USDT',
      size: -79,
      left: 40,
      price: 1900,
      tif: 'gtc',
      reduce_only: true,
      status: 'open',
    })
  })

  it('撤单 URL 对合约与订单 ID 进行编码', async () => {
    const fetchMock = vi.fn(async () =>
      new Response(
        JSON.stringify({
          id: 'id/with space',
          contract: 'BTC_USDT',
          status: 'finished',
          finish_as: 'cancelled',
          warning: '',
        }),
        { status: 200 },
      ),
    )
    vi.stubGlobal('fetch', fetchMock)

    await httpApi.cancelOpenOrder('BTC_USDT', 'id/with space')

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/orders/BTC_USDT/id%2Fwith%20space',
      expect.objectContaining({ method: 'DELETE' }),
    )
  })
})
