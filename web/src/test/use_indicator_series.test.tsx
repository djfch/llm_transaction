/**
 * useIndicatorSeries 测试：先拉短名单配置、再按完整短名单（含 scalar）显式传 keys 拉序列；
 * WS indicator_config_updated → 重拉配置（内容变化连带重拉序列）；
 * 当前合约 ticker → 节流重拉序列（15s 最小间隔）；
 * 切换合约/周期后旧响应隔离（加载中/失败均不残留旧指标）。
 */
import { renderHook, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { Candle, IndicatorConfig, IndicatorSeriesResponse, WsMessage } from '../api/types'
import { useIndicatorSeries } from '../hooks/useIndicatorSeries'

const holder = vi.hoisted(() => ({
  lastMessage: null as WsMessage | null,
  getIndicatorConfig: vi.fn<() => Promise<IndicatorConfig>>(),
  getIndicatorSeries:
    vi.fn<(c: string, i: string, k: string[], l?: number) => Promise<IndicatorSeriesResponse>>(),
}))

vi.mock('../api', () => ({
  api: {
    getIndicatorConfig: () => holder.getIndicatorConfig(),
    getIndicatorSeries: (c: string, i: string, k: string[], l?: number) =>
      holder.getIndicatorSeries(c, i, k, l),
  },
}))

// WS 可控桩：测试改写 holder.lastMessage 后 rerender 即可派发消息
vi.mock('../hooks/useWs', () => ({
  useWs: () => ({ connected: true, lastMessage: holder.lastMessage }),
}))

const CONFIG: IndicatorConfig = {
  shortlist: ['ema20', 'rsi14', 'atr14', 'oi'],
  available: [
    { key: 'ema20', label: 'EMA20(指数均线)', kind: 'overlay', fields: ['ema20'] },
    { key: 'rsi14', label: 'RSI14(相对强弱)', kind: 'pane', fields: ['rsi14'] },
    { key: 'atr14', label: 'ATR14(平均真实波幅)', kind: 'scalar', fields: ['atr14'] },
    { key: 'oi', label: '持仓量', kind: 'scalar', fields: [] },
  ],
}

const CANDLES: Candle[] = [
  { t: 100, o: 1, h: 1, l: 1, c: 1, v: 1 },
  { t: 200, o: 1, h: 1, l: 1, c: 1, v: 1 },
  { t: 300, o: 1, h: 1, l: 1, c: 1, v: 1 },
]

const SERIES: IndicatorSeriesResponse = {
  contract: 'BTC_USDT',
  interval: '1h',
  series: {
    ema20: {
      label: 'EMA20(指数均线)',
      kind: 'overlay',
      fields: { ema20: [{ time: 100, value: 1 }, { time: 200, value: null }, { time: 300, value: 3 }] },
      current: null,
    },
    rsi14: {
      label: 'RSI14(相对强弱)',
      kind: 'pane',
      fields: { rsi14: [{ time: 100, value: 55 }, { time: 200, value: 60 }, { time: 300, value: 65 }] },
      current: null,
    },
    atr14: {
      label: 'ATR14(平均真实波幅)',
      kind: 'scalar',
      fields: { atr14: [{ time: 300, value: 12.5 }] },
      current: null,
    },
    oi: { label: '持仓量', kind: 'scalar', fields: {}, current: 123456 },
  },
}

function renderSeries(contract = 'BTC_USDT') {
  return renderHook((props: { contract: string }) => useIndicatorSeries(props.contract, '1h', CANDLES, 200), {
    initialProps: { contract },
  })
}

/** 等首组数据（配置 + 序列）加载完成 */
async function waitLoaded(result: { current: { shortlist: string[] } }) {
  await waitFor(() => expect(result.current.shortlist).toEqual(['EMA20', 'RSI14', 'ATR14', '持仓量']))
}

beforeEach(() => {
  holder.lastMessage = null
  holder.getIndicatorConfig.mockReset()
  holder.getIndicatorSeries.mockReset()
  holder.getIndicatorConfig.mockResolvedValue(CONFIG)
  holder.getIndicatorSeries.mockResolvedValue(SERIES)
})

describe('useIndicatorSeries', () => {
  it('先拉配置，再按完整短名单（含 scalar）显式传 keys 拉序列，并组装 overlays/panes/badges/shortlist', async () => {
    const { result } = renderSeries()
    await waitLoaded(result)

    // keys = 完整短名单（scalar 也传：atr14 有序列、oi 随响应返回 current，徽标才有数据）
    expect(holder.getIndicatorSeries).toHaveBeenCalledWith(
      'BTC_USDT',
      '1h',
      ['ema20', 'rsi14', 'atr14', 'oi'],
      200,
    )
    // overlay：ema20 一条线，null 跳过、按 K 线 time 对齐
    expect(result.current.overlays).toHaveLength(1)
    expect(result.current.overlays[0].id).toBe('ema20.ema20')
    expect(result.current.overlays[0].data).toEqual([
      { time: 100, value: 1 },
      { time: 300, value: 3 },
    ])
    // pane：rsi14 一个副图一条 Line
    expect(result.current.panes).toHaveLength(1)
    expect(result.current.panes[0].key).toBe('rsi14')
    expect(result.current.panes[0].lines[0].data).toHaveLength(3)
    // scalar 徽标：atr14 取序列末值、oi 取 current
    expect(result.current.badges).toEqual([
      { key: 'atr14', label: 'ATR14', text: '12.5' },
      { key: 'oi', label: '持仓量', text: '123,456' },
    ])
  })

  it('WS indicator_config_updated → 重拉配置；内容变化连带重拉序列', async () => {
    // 第二次起返回新引用（模拟短名单内容变化；同引用时 React 跳过重渲染，无需重拉序列）
    holder.getIndicatorConfig.mockResolvedValueOnce(CONFIG).mockImplementation(() =>
      Promise.resolve({ ...CONFIG }),
    )
    const { result, rerender } = renderSeries()
    await waitLoaded(result)
    expect(holder.getIndicatorConfig).toHaveBeenCalledTimes(1)
    expect(holder.getIndicatorSeries).toHaveBeenCalledTimes(1)

    holder.lastMessage = { type: 'indicator_config_updated' }
    rerender({ contract: 'BTC_USDT' })

    await waitFor(() => expect(holder.getIndicatorConfig).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(holder.getIndicatorSeries).toHaveBeenCalledTimes(2))
  })

  it('当前合约 ticker → 节流重拉序列（15s 内重复 ticker 不再重拉）；其他合约 ticker 忽略', async () => {
    const { result, rerender } = renderSeries()
    await waitLoaded(result)
    expect(holder.getIndicatorSeries).toHaveBeenCalledTimes(1)

    // 其他合约 ticker：忽略
    holder.lastMessage = { type: 'ticker', data: { contract: 'ETH_USDT', last: 3000 } }
    rerender({ contract: 'BTC_USDT' })
    await new Promise((resolve) => setTimeout(resolve, 20))
    expect(holder.getIndicatorSeries).toHaveBeenCalledTimes(1)

    // 当前合约 ticker：首次触发重拉
    holder.lastMessage = { type: 'ticker', data: { contract: 'BTC_USDT', last: 115000 } }
    rerender({ contract: 'BTC_USDT' })
    await waitFor(() => expect(holder.getIndicatorSeries).toHaveBeenCalledTimes(2))

    // 15s 节流窗口内的第二条 ticker：不再重拉
    holder.lastMessage = { type: 'ticker', data: { contract: 'BTC_USDT', last: 115001 } }
    rerender({ contract: 'BTC_USDT' })
    await new Promise((resolve) => setTimeout(resolve, 20))
    expect(holder.getIndicatorSeries).toHaveBeenCalledTimes(2)
  })

  it('合约为空时不发请求，结果为空态', async () => {
    const { result } = renderSeries('')
    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(holder.getIndicatorSeries).not.toHaveBeenCalled()
    expect(result.current.overlays).toEqual([])
    expect(result.current.panes).toEqual([])
  })

  it('切换合约后旧响应被隔离：新请求挂起期间不残留旧合约指标', async () => {
    const { result, rerender } = renderSeries()
    await waitLoaded(result)
    expect(result.current.overlays).toHaveLength(1)

    // 切到 ETH：请求挂起不 resolve，旧 BTC 数据立即不可见（相同时间戳也不串图）
    let resolveEth: (v: IndicatorSeriesResponse) => void = () => {}
    holder.getIndicatorSeries.mockImplementation(
      () =>
        new Promise<IndicatorSeriesResponse>((res) => {
          resolveEth = res
        }),
    )
    rerender({ contract: 'ETH_USDT' })
    await waitFor(() => expect(result.current.overlays).toEqual([]))
    expect(result.current.panes).toEqual([])
    // 徽标不残留旧数值（条目仍在、值降级为「无数据」）
    expect(result.current.badges.every((b) => b.text === '无数据')).toBe(true)

    // ETH 响应到达后正常展示
    resolveEth({ ...SERIES, contract: 'ETH_USDT' })
    await waitFor(() => expect(result.current.overlays).toHaveLength(1))
  })

  it('切换合约后请求失败：不残留旧合约指标，error 透出', async () => {
    const { result, rerender } = renderSeries()
    await waitLoaded(result)
    expect(result.current.overlays).toHaveLength(1)

    holder.getIndicatorSeries.mockRejectedValue(new Error('boom'))
    rerender({ contract: 'ETH_USDT' })
    await waitFor(() => expect(result.current.error).toBe('boom'))
    expect(result.current.overlays).toEqual([])
    expect(result.current.panes).toEqual([])
    expect(result.current.badges.every((b) => b.text === '无数据')).toBe(true)
  })
})
