import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { PortfolioSnapshot, WsMessage } from '../api/types'
import { useLivePortfolio } from '../hooks/useLivePortfolio'

const holder = vi.hoisted(() => ({
  lastMessage: null as WsMessage | null,
  getPortfolio: vi.fn<() => Promise<PortfolioSnapshot>>(),
}))

vi.mock('../api', () => ({
  api: { getPortfolio: () => holder.getPortfolio() },
}))

vi.mock('../hooks/useWs', () => ({
  useWs: () => ({ connected: true, lastMessage: holder.lastMessage }),
}))

const SNAPSHOT: PortfolioSnapshot = {
  asOf: '2026-07-24T00:00:00.000Z',
  account: { equity: 10100, available: 9900, unrealised_pnl: 100 },
  positions: [],
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => {
    resolve = done
  })
  return { promise, resolve }
}

beforeEach(() => {
  vi.useFakeTimers()
  vi.setSystemTime(new Date('2026-07-24T00:00:00.000Z'))
  holder.lastMessage = null
  holder.getPortfolio.mockReset()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('useLivePortfolio', () => {
  it('高频 ticker 只触发每秒一次尾随刷新，且请求不并发', async () => {
    const first = deferred<PortfolioSnapshot>()
    holder.getPortfolio
      .mockReturnValueOnce(first.promise)
      .mockResolvedValueOnce({ ...SNAPSHOT, account: { ...SNAPSHOT.account, equity: 10120 } })
    const { rerender, result } = renderHook(() => useLivePortfolio())

    await act(async () => vi.advanceTimersByTimeAsync(0))
    expect(holder.getPortfolio).toHaveBeenCalledTimes(1)

    holder.lastMessage = { type: 'ticker', data: { contract: 'BTC_USDT', last: 100 } }
    rerender()
    holder.lastMessage = { type: 'ticker', data: { contract: 'ETH_USDT', last: 200 } }
    rerender()
    expect(holder.getPortfolio).toHaveBeenCalledTimes(1)

    await act(async () => first.resolve(SNAPSHOT))
    await act(async () => vi.advanceTimersByTimeAsync(999))
    expect(holder.getPortfolio).toHaveBeenCalledTimes(1)

    await act(async () => vi.advanceTimersByTimeAsync(1))
    expect(holder.getPortfolio).toHaveBeenCalledTimes(2)
    expect(result.current.data?.account.equity).toBe(10120)
  })

  it('刷新失败保留最后一次成功快照并暴露错误', async () => {
    holder.getPortfolio
      .mockResolvedValueOnce(SNAPSHOT)
      .mockRejectedValueOnce(new Error('组合快照暂不可用'))
    const { rerender, result } = renderHook(() => useLivePortfolio())
    await act(async () => vi.advanceTimersByTimeAsync(0))
    expect(result.current.data).toEqual(SNAPSHOT)

    holder.lastMessage = { type: 'ticker', data: { contract: 'BTC_USDT', last: 101 } }
    rerender()
    await act(async () => vi.advanceTimersByTimeAsync(1000))

    expect(result.current.data).toEqual(SNAPSHOT)
    expect(result.current.error).toBe('组合快照暂不可用')
  })

  it('决策轮事件会抢占 ticker 的尾随定时器并立即刷新', async () => {
    holder.getPortfolio.mockResolvedValue(SNAPSHOT)
    const { rerender } = renderHook(() => useLivePortfolio())
    await act(async () => vi.advanceTimersByTimeAsync(0))

    holder.lastMessage = { type: 'ticker', data: { contract: 'BTC_USDT', last: 101 } }
    rerender()
    expect(holder.getPortfolio).toHaveBeenCalledTimes(1)

    holder.lastMessage = {
      type: 'round',
      data: { round_id: 'round-1', ok: true, wake_source: '价格触发' },
    }
    rerender()
    await act(async () => vi.advanceTimersByTimeAsync(0))

    expect(holder.getPortfolio).toHaveBeenCalledTimes(2)
  })
})
