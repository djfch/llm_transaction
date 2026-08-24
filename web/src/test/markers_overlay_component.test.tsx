import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { WsMessage } from '../api/types'
import MarkersOverlay from '../components/console/MarkersOverlay'

const holder = vi.hoisted(() => ({
  focus: vi.fn(),
  getTrades: vi.fn(),
  lastMessage: null as WsMessage | null,
}))

/** 固定夹具：1 笔有归属买单（round-buy） */
const BUY_TRADE = {
  id: 7,
  round_id: 'round-buy',
  time: '2024-01-01T00:42:00Z',
  contract: 'BTC_USDT',
  size: 1,
  price: 100,
  fee: 0,
  pnl: 0,
  source: 'llm_open',
}

vi.mock('../api', () => ({
  api: { getTrades: holder.getTrades },
}))

// WS 可控桩：测试改写 holder.lastMessage 后 rerender 即可派发消息
vi.mock('../hooks/useWs', () => ({
  useWs: () => ({ connected: true, lastMessage: holder.lastMessage }),
}))

vi.mock('../hooks/useRoundFocus', () => ({
  useRoundFocus: () => ({ focus: holder.focus }),
}))

function chartAt(x: number, width = 400) {
  return {
    timeScale: () => ({
      timeToCoordinate: () => x,
      width: () => width,
      subscribeVisibleLogicalRangeChange: vi.fn(),
      unsubscribeVisibleLogicalRangeChange: vi.fn(),
      subscribeSizeChange: vi.fn(),
      unsubscribeSizeChange: vi.fn(),
    }),
  }
}

function seriesHarness(initialY: number, paneHeight = 300) {
  let y = initialY
  let dataChanged: (() => void) | null = null
  const unsubscribeDataChanged = vi.fn()
  return {
    api: {
      priceToCoordinate: () => y,
      priceScale: () => ({
        options: () => ({ scaleMargins: { top: 0.08, bottom: 0.26 } }),
      }),
      getPane: () => ({ getHeight: () => paneHeight }),
      subscribeDataChanged: (handler: () => void) => {
        dataChanged = handler
      },
      unsubscribeDataChanged,
    },
    setY: (next: number) => {
      y = next
    },
    emitDataChanged: () => dataChanged?.(),
    unsubscribeDataChanged,
  }
}

const BARS = [{ t: 1_704_067_200, o: 100, h: 105, l: 95, c: 101, v: 10 }]

function overlayUi(chart: unknown, series: unknown) {
  return (
    <MarkersOverlay
      chart={chart as never}
      series={series as never}
      bars={BARS}
      contract="BTC_USDT"
      intervalSec={3600}
    />
  )
}

describe('MarkersOverlay', () => {
  beforeEach(() => {
    holder.focus.mockReset()
    holder.getTrades.mockReset()
    holder.getTrades.mockResolvedValue({ items: [BUY_TRADE], total: 1, offset: 0, limit: 100 })
    holder.lastMessage = null
    Object.defineProperty(HTMLElement.prototype, 'clientWidth', {
      configurable: true,
      get: () => 400,
    })
    Object.defineProperty(HTMLElement.prototype, 'clientHeight', {
      configurable: true,
      get: () => 300,
    })
    vi.stubGlobal(
      'ResizeObserver',
      class {
        observe() {}
        disconnect() {}
      },
    )
  })

  it('保留原圆形徽标样式，点击定位决策轮', async () => {
    const series = seriesHarness(100)
    const view = render(overlayUi(chartAt(100), series.api))

    const marker = await screen.findByRole('button', { name: /买入成交/ })
    expect(marker).toHaveClass('rounded-full', 'border', 'bg-emerald-500/25')
    expect(view.getByTestId('markers-overlay')).toHaveClass('overflow-hidden')
    fireEvent.click(marker)
    expect(holder.focus).toHaveBeenCalledWith('round-buy')
  })

  it('成交请求带当前合约过滤；WS trades_updated 事件 → 重拉', async () => {
    const view = render(overlayUi(chartAt(100), seriesHarness(100).api))
    await screen.findByRole('button', { name: /买入成交/ })
    expect(holder.getTrades).toHaveBeenCalledTimes(1)
    expect(holder.getTrades).toHaveBeenLastCalledWith(0, 100, 'BTC_USDT')

    holder.lastMessage = { type: 'trades_updated', data: { contracts: ['BTC_USDT'], count: 1 } }
    view.rerender(overlayUi(chartAt(100), seriesHarness(100).api))

    await waitFor(() => expect(holder.getTrades).toHaveBeenCalledTimes(2))
    expect(holder.getTrades).toHaveBeenLastCalledWith(0, 100, 'BTC_USDT')
  })

  it('WS round 事件不再触发标记重拉（trades_updated 已接管失效信号）', async () => {
    const view = render(overlayUi(chartAt(100), seriesHarness(100).api))
    await screen.findByRole('button', { name: /买入成交/ })
    expect(holder.getTrades).toHaveBeenCalledTimes(1)

    holder.lastMessage = {
      type: 'round',
      data: { round_id: 'r-new', ok: true, wake_source: '价格触发' },
    }
    view.rerender(overlayUi(chartAt(100), seriesHarness(100).api))

    // 等一拍确保 effect 有机会执行：round 不应触发重拉
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(holder.getTrades).toHaveBeenCalledTimes(1)
  })

  it('无归属决策轮的标记照常绘制但不可点击（span 渲染、无按钮）', async () => {
    holder.getTrades.mockResolvedValue({
      items: [{ ...BUY_TRADE, id: 8, round_id: '', size: -1, source: 'liquidation' }],
      total: 1,
      offset: 0,
      limit: 100,
    })
    render(overlayUi(chartAt(100), seriesHarness(100).api))

    const marker = await screen.findByTitle(/卖出成交 @ 100 · 无归属决策轮/)
    expect(marker.tagName).toBe('SPAN')
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
  })

  it('标记落在主价格绘图区之外但面板之内（留白/成交量带）照常显示', async () => {
    // y=210 落在底部 26% 成交量带内（旧语义隐藏，新语义保留）
    render(overlayUi(chartAt(100), seriesHarness(210).api))
    await screen.findByRole('button', { name: /买入成交/ })
  })

  it('标记圆越出面板上下沿时隐藏，缩放回到面板内重新显示', async () => {
    const series = seriesHarness(280)
    const view = render(overlayUi(chartAt(100), series.api))
    // 买单标记 y=280+16=296，越出面板下沿（300-8=292）→ 隐藏
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: /买入成交/ })).not.toBeInTheDocument(),
    )

    // 模拟缩小图表：价格坐标回到 240，标记 y=256 回到面板内 → 重新显示
    act(() => {
      series.setY(240)
      series.emitDataChanged()
    })
    await screen.findByRole('button', { name: /买入成交/ })
    view.unmount()
  })

  it('标记圆越出时间轴两端时隐藏，拖回可视区重新显示', async () => {
    const view = render(overlayUi(chartAt(100, 200), seriesHarness(100, 270).api))
    await screen.findByRole('button', { name: /买入成交/ })
    view.rerender(overlayUi(chartAt(250, 200), seriesHarness(100, 270).api))
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: /买入成交/ })).not.toBeInTheDocument(),
    )

    view.rerender(overlayUi(chartAt(100, 200), seriesHarness(100, 270).api))
    await screen.findByRole('button', { name: /买入成交/ })
  })

  it('卖在最高点：标记贴高点上方，越出面板顶沿时隐藏、缩小时重新显示', async () => {
    holder.getTrades.mockResolvedValue({
      items: [{ ...BUY_TRADE, id: 9, size: -1, source: 'llm_close' }],
      total: 1,
      offset: 0,
      limit: 100,
    })
    // 卖单标记锚定 bar.h=105：priceY=20 → 标记 y=4，越出面板顶沿（半径 8）→ 隐藏
    const series = seriesHarness(20)
    render(overlayUi(chartAt(100), series.api))
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: /卖出成交/ })).not.toBeInTheDocument(),
    )

    // 缩小图表：priceY=30 → 标记 y=14 进入面板 → 显示
    act(() => {
      series.setY(30)
      series.emitDataChanged()
    })
    await screen.findByRole('button', { name: /卖出成交/ })
  })

  it('ticker 数据更新后重算纵坐标，并在卸载时清理订阅', async () => {
    const series = seriesHarness(100)
    const view = render(overlayUi(chartAt(100), series.api))
    const marker = await screen.findByRole('button', { name: /买入成交/ })
    expect(marker).toHaveStyle({ top: '116px' })

    act(() => {
      series.setY(140)
      series.emitDataChanged()
    })
    await waitFor(() => expect(marker).toHaveStyle({ top: '156px' }))

    view.unmount()
    expect(series.unsubscribeDataChanged).toHaveBeenCalled()
  })

  it('价格轴交互后重新读取价格坐标', async () => {
    const series = seriesHarness(100)
    const view = render(overlayUi(chartAt(100), series.api))
    const marker = await screen.findByRole('button', { name: /买入成交/ })
    series.setY(140)
    fireEvent.pointerMove(view.container)

    await waitFor(() => expect(marker).toHaveStyle({ top: '156px' }))
  })
})
