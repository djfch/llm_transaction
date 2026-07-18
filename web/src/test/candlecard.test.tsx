/**
 * K 线卡测试（H1 回归）：watchlist 失败时展示错误而非永久"加载中"；
 * 空白名单走空态；正常时默认选中第一个合约并按当前周期取数；
 * WS ticker 推送驱动头部实时最新价（仅当前选中合约）。
 * mock 采用"每个用例新建 vi.fn"持有器模式（规避 vitest 对清理后 mock 拒绝的误报）。
 */
import { render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { Candle, Watchlist } from '../api/types'
import CandleCard from '../components/CandleCard'

// jsdom 无法运行 lightweight-charts（依赖 canvas），替换为占位组件
vi.mock('../components/CandleChart', () => ({
  default: () => <div data-testid="candle-chart-stub" />,
}))

const holder = vi.hoisted(() => ({
  getWatchlist: vi.fn() as ReturnType<typeof vi.fn<() => Promise<Watchlist>>>,
  getCandles: vi.fn() as ReturnType<typeof vi.fn<(c: string, i: string, l?: number) => Promise<Candle[]>>>,
  lastMessage: null as import('../api/types').WsMessage | null,
}))
vi.mock('../api', () => ({
  api: {
    getWatchlist: () => holder.getWatchlist(),
    getCandles: (c: string, i: string, l?: number) => holder.getCandles(c, i, l),
  },
}))
// 隔离真实 WS（jsdom 无 WebSocket）：lastMessage 由各用例自行注入
vi.mock('../hooks/useWs', () => ({
  useWs: () => ({ connected: true, lastMessage: holder.lastMessage }),
}))

afterEach(() => {
  holder.lastMessage = null
})

const oneCandle: Candle = { t: 1_700_000_000, o: 1, h: 2, l: 0.5, c: 1.5, v: 10 }

/** 常规夹具：两个合约 + 一根 K 线 */
function mockNormal() {
  holder.getWatchlist = vi
    .fn<() => Promise<Watchlist>>()
    .mockResolvedValue({ settle: 'usdt', contracts: ['BTC_USDT', 'ETH_USDT'] })
  holder.getCandles = vi
    .fn<(c: string, i: string, l?: number) => Promise<Candle[]>>()
    .mockResolvedValue([oneCandle])
}

describe('CandleCard(K线卡片)', () => {
  it('watchlist 加载失败：展示错误原因，而非永久加载中', async () => {
    holder.getWatchlist = vi.fn<() => Promise<Watchlist>>().mockRejectedValue(new Error('网络错误'))
    holder.getCandles = vi.fn()
    render(<CandleCard />)

    expect(await screen.findByText('加载失败：网络错误')).toBeInTheDocument()
    expect(screen.queryByText('加载中…')).not.toBeInTheDocument()
    expect(holder.getCandles).not.toHaveBeenCalled()
  })

  it('白名单为空：展示暂无数据', async () => {
    holder.getWatchlist = vi.fn<() => Promise<Watchlist>>().mockResolvedValue({ settle: 'usdt', contracts: [] })
    holder.getCandles = vi.fn()
    render(<CandleCard />)

    expect(await screen.findByText('暂无数据')).toBeInTheDocument()
    expect(holder.getCandles).not.toHaveBeenCalled()
  })

  it('正常加载：默认选中第一个合约并按 1h/200 取数渲染', async () => {
    mockNormal()
    render(<CandleCard />)

    expect(await screen.findByTestId('candle-chart-stub')).toBeInTheDocument()
    expect(holder.getCandles).toHaveBeenCalledWith('BTC_USDT', '1h', 200)
  })

  it('WS ticker 推送当前选中合约：头部显示实时最新价', async () => {
    mockNormal()
    const { rerender } = render(<CandleCard />)
    await screen.findByTestId('candle-chart-stub')
    expect(screen.queryByTestId('live-price')).not.toBeInTheDocument()

    holder.lastMessage = { type: 'ticker', data: { contract: 'BTC_USDT', last: 64095.7 } }
    rerender(<CandleCard />)

    const live = await screen.findByTestId('live-price')
    expect(live.textContent).toContain('64,095.70')
  })

  it('WS ticker 推送其他合约：不显示最新价（防串合约）', async () => {
    mockNormal()
    const { rerender } = render(<CandleCard />)
    await screen.findByTestId('candle-chart-stub')

    holder.lastMessage = { type: 'ticker', data: { contract: 'ETH_USDT', last: 1844.4 } }
    rerender(<CandleCard />)

    // 默认选中 BTC_USDT，ETH 的推送不应上屏
    expect(screen.queryByTestId('live-price')).not.toBeInTheDocument()
  })

  it('WS ticker 多合约交替推送：选中合约实时价持续显示不闪烁（回归 P1-1）', async () => {
    mockNormal()
    const { rerender } = render(<CandleCard />)
    await screen.findByTestId('candle-chart-stub')

    // BTC 推送 → 显示 BTC 价
    holder.lastMessage = { type: 'ticker', data: { contract: 'BTC_USDT', last: 64095.7 } }
    rerender(<CandleCard />)
    expect((await screen.findByTestId('live-price')).textContent).toContain('64,095.70')

    // ETH 推送插进来 → BTC 价不消失（此前 live 被覆盖导致闪烁）
    holder.lastMessage = { type: 'ticker', data: { contract: 'ETH_USDT', last: 1844.4 } }
    rerender(<CandleCard />)
    expect(screen.getByTestId('live-price').textContent).toContain('64,095.70')

    // BTC 再推 → 更新为新价
    holder.lastMessage = { type: 'ticker', data: { contract: 'BTC_USDT', last: 64100.2 } }
    rerender(<CandleCard />)
    expect(screen.getByTestId('live-price').textContent).toContain('64,100.20')
  })
})
