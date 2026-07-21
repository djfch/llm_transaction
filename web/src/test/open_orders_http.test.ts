import { afterEach, describe, expect, it, vi } from 'vitest'
import { httpApi } from '../api/http'

afterEach(() => vi.unstubAllGlobals())

describe('open_orders HTTP \u9002\u914d', () => {
  it('\u5c06\u540e\u7aef Decimal \u5b57\u7b26\u4e32\u8f6c\u4e3a\u6570\u5b57', async () => {
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

  it('\u64a4\u5355 URL \u5bf9\u5408\u7ea6\u4e0e\u8ba2\u5355 ID \u8fdb\u884c\u7f16\u7801', async () => {
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
