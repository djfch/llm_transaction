/**
 * K线买卖点标记纯函数测试：合约/归属过滤、买卖方向映射、时间换算与排序、非法时间剔除。
 */
import { describe, expect, it } from 'vitest'
import type { Trade } from '../api/types'
import { buildTradeMarkers, tradeTimeSec } from '../utils/klineMarkers'

/** 构造一笔成交（默认 BTC 买单有归属，按 partial 覆盖） */
function trade(partial: Partial<Trade>): Trade {
  return {
    id: 1,
    round_id: 'r1',
    time: '2024-01-01T00:00:00.000Z',
    contract: 'BTC_USDT',
    size: 1,
    price: 100,
    fee: 0.1,
    pnl: 0,
    source: 'llm_open',
    ...partial,
  }
}

describe('tradeTimeSec(成交时间换算)', () => {
  it('ISO → Unix 秒（毫秒向下取整）', () => {
    expect(tradeTimeSec('1970-01-01T00:00:10.999Z')).toBe(10)
    expect(tradeTimeSec('2024-01-01T00:00:00.000Z')).toBe(1_704_067_200)
  })

  it('非法时间 → null', () => {
    expect(tradeTimeSec('not-a-date')).toBeNull()
    expect(tradeTimeSec('')).toBeNull()
  })
})

describe('buildTradeMarkers(成交→标记映射)', () => {
  it('仅保留当前合约且 round_id 非空的成交', () => {
    const ms = buildTradeMarkers(
      [
        trade({ id: 1, contract: 'BTC_USDT' }),
        trade({ id: 2, contract: 'ETH_USDT' }),
        trade({ id: 3, contract: 'BTC_USDT', round_id: '' }),
      ],
      'BTC_USDT',
    )
    expect(ms.map((m) => m.id)).toEqual([1])
    expect(ms[0].roundId).toBe('r1')
    expect(ms[0].price).toBe(100)
    expect(ms[0].timeSec).toBe(1_704_067_200)
  })

  it('size 正买负卖 → side buy/sell', () => {
    const ms = buildTradeMarkers(
      [
        trade({ id: 1, size: 5 }),
        trade({ id: 2, size: -5, time: '2024-01-01T01:00:00.000Z' }),
      ],
      'BTC_USDT',
    )
    expect(ms.find((m) => m.id === 1)?.side).toBe('buy')
    expect(ms.find((m) => m.id === 2)?.side).toBe('sell')
  })

  it('非法时间剔除 + 结果按时间升序', () => {
    const ms = buildTradeMarkers(
      [
        trade({ id: 1, time: '2024-01-02T00:00:00.000Z' }),
        trade({ id: 2, time: 'bad' }),
        trade({ id: 3, time: '2024-01-01T00:00:00.000Z' }),
      ],
      'BTC_USDT',
    )
    expect(ms.map((m) => m.id)).toEqual([3, 1])
  })

  it('空列表 / 无匹配合约 → 空数组', () => {
    expect(buildTradeMarkers([], 'BTC_USDT')).toEqual([])
    expect(buildTradeMarkers([trade({ id: 1 })], 'ETH_USDT')).toEqual([])
  })
})
