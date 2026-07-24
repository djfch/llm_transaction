import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import MarkersOverlay from '../components/console/MarkersOverlay'

const holder = vi.hoisted(() => ({ focus: vi.fn() }))

vi.mock('../api', () => ({
  api: {
    getTrades: () =>
      Promise.resolve({
        items: [
          {
            id: 7,
            round_id: 'round-buy',
            time: '2024-01-01T00:42:00Z',
            contract: 'BTC_USDT',
            size: 1,
            price: 100,
            fee: 0,
            pnl: 0,
            source: 'llm_open',
          },
        ],
        total: 1,
        offset: 0,
        limit: 100,
      }),
  },
}))

vi.mock('../hooks/useWs', () => ({
  useWs: () => ({ connected: true, lastMessage: null }),
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

describe('MarkersOverlay', () => {
  beforeEach(() => {
    holder.focus.mockReset()
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
    const view = render(
      <MarkersOverlay
        chart={chartAt(100) as never}
        series={series.api as never}
        bars={[{ t: 1_704_067_200, o: 100, h: 105, l: 95, c: 101, v: 10 }]}
        contract="BTC_USDT"
        intervalSec={3600}
      />,
    )

    const marker = await screen.findByRole('button', { name: /买入\/开多/ })
    expect(marker).toHaveClass('rounded-full', 'border', 'bg-emerald-500/25')
    expect(view.getByTestId('markers-overlay')).toHaveClass('overflow-hidden')
    fireEvent.click(marker)
    expect(holder.focus).toHaveBeenCalledWith('round-buy')
  })

  it('标记中心越过主价格绘图区时自动隐藏', async () => {
    const series = seriesHarness(100)
    const view = render(
      <MarkersOverlay
        chart={chartAt(100) as never}
        series={series.api as never}
        bars={[{ t: 1_704_067_200, o: 100, h: 105, l: 95, c: 101, v: 10 }]}
        contract="BTC_USDT"
        intervalSec={3600}
      />,
    )
    await screen.findByRole('button', { name: /买入\/开多/ })

    view.rerender(
      <MarkersOverlay
        chart={chartAt(100) as never}
        series={seriesHarness(210).api as never}
        bars={[{ t: 1_704_067_200, o: 100, h: 105, l: 95, c: 101, v: 10 }]}
        contract="BTC_USDT"
        intervalSec={3600}
      />,
    )

    await waitFor(() =>
      expect(screen.queryByRole('button', { name: /买入\/开多/ })).not.toBeInTheDocument(),
    )
  })

  it('使用时间轴宽度与 pane 高度裁剪价格轴和成交量区域', async () => {
    const view = render(
      <MarkersOverlay
        chart={chartAt(100, 200) as never}
        series={seriesHarness(100, 270).api as never}
        bars={[{ t: 1_704_067_200, o: 100, h: 105, l: 95, c: 101, v: 10 }]}
        contract="BTC_USDT"
        intervalSec={3600}
      />,
    )
    await screen.findByRole('button', { name: /买入\/开多/ })
    view.rerender(
      <MarkersOverlay
        chart={chartAt(250, 200) as never}
        series={seriesHarness(100, 270).api as never}
        bars={[{ t: 1_704_067_200, o: 100, h: 105, l: 95, c: 101, v: 10 }]}
        contract="BTC_USDT"
        intervalSec={3600}
      />,
    )
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: /买入\/开多/ })).not.toBeInTheDocument(),
    )

    view.rerender(
      <MarkersOverlay
        chart={chartAt(100, 200) as never}
        series={seriesHarness(100, 270).api as never}
        bars={[{ t: 1_704_067_200, o: 100, h: 105, l: 95, c: 101, v: 10 }]}
        contract="BTC_USDT"
        intervalSec={3600}
      />,
    )
    await screen.findByRole('button', { name: /买入\/开多/ })
    view.rerender(
      <MarkersOverlay
        chart={chartAt(100, 200) as never}
        series={seriesHarness(190, 270).api as never}
        bars={[{ t: 1_704_067_200, o: 100, h: 105, l: 95, c: 101, v: 10 }]}
        contract="BTC_USDT"
        intervalSec={3600}
      />,
    )
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: /买入\/开多/ })).not.toBeInTheDocument(),
    )
  })

  it('ticker 数据更新后重算纵坐标，并在卸载时清理订阅', async () => {
    const series = seriesHarness(100)
    const view = render(
      <MarkersOverlay
        chart={chartAt(100) as never}
        series={series.api as never}
        bars={[{ t: 1_704_067_200, o: 100, h: 105, l: 95, c: 101, v: 10 }]}
        contract="BTC_USDT"
        intervalSec={3600}
      />,
    )
    const marker = await screen.findByRole('button', { name: /买入\/开多/ })
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
    const view = render(
      <MarkersOverlay
        chart={chartAt(100) as never}
        series={series.api as never}
        bars={[{ t: 1_704_067_200, o: 100, h: 105, l: 95, c: 101, v: 10 }]}
        contract="BTC_USDT"
        intervalSec={3600}
      />,
    )
    const marker = await screen.findByRole('button', { name: /买入\/开多/ })
    series.setY(140)
    fireEvent.pointerMove(view.container)

    await waitFor(() => expect(marker).toHaveStyle({ top: '156px' }))
  })
})
