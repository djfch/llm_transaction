/**
 * K 线卡测试（H1 回归）：watchlist 失败时展示错误而非永久"加载中"；
 * 空白名单走空态；正常时默认选中第一个合约并按当前周期取数。
 * mock 采用"每个用例新建 vi.fn"持有器模式（规避 vitest 对清理后 mock 拒绝的误报）。
 */
import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { Candle, Watchlist } from '../api/types'
import CandleCard from '../components/CandleCard'

// jsdom 无法运行 lightweight-charts（依赖 canvas），替换为占位组件
vi.mock('../components/CandleChart', () => ({
  default: () => <div data-testid="candle-chart-stub" />,
}))

const holder = vi.hoisted(() => ({
  getWatchlist: vi.fn() as ReturnType<typeof vi.fn<() => Promise<Watchlist>>>,
  getCandles: vi.fn() as ReturnType<typeof vi.fn<(c: string, i: string, l?: number) => Promise<Candle[]>>>,
}))
vi.mock('../api', () => ({
  api: {
    getWatchlist: () => holder.getWatchlist(),
    getCandles: (c: string, i: string, l?: number) => holder.getCandles(c, i, l),
  },
}))

const oneCandle: Candle = { t: 1_700_000_000, o: 1, h: 2, l: 0.5, c: 1.5, v: 10 }

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
    holder.getWatchlist = vi
      .fn<() => Promise<Watchlist>>()
      .mockResolvedValue({ settle: 'usdt', contracts: ['BTC_USDT', 'ETH_USDT'] })
    holder.getCandles = vi.fn<(c: string, i: string, l?: number) => Promise<Candle[]>>().mockResolvedValue([oneCandle])
    render(<CandleCard />)

    expect(await screen.findByTestId('candle-chart-stub')).toBeInTheDocument()
    expect(holder.getCandles).toHaveBeenCalledWith('BTC_USDT', '1h', 200)
  })
})
