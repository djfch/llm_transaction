/**
 * useIndicatorChart 测试（假 IChartApi）：
 * overlay 增量同步（创建/移除/仅刷数据）；pane 签名变化整体重建（removeSeries→removePane→addPane），
 * 同签名仅 setData；图表高度随副图数量调整。
 */
import { renderHook } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { HistogramSeries, LineSeries, type IChartApi } from 'lightweight-charts'
import { useIndicatorChart } from '../hooks/useIndicatorChart'
import type { OverlayLine, PaneLine, PaneSpec } from '../utils/indicatorSeries'
import type { UTCTimestamp } from 'lightweight-charts'

/** 假图表：记录全部调用，addSeries/addPane 返回带 setData/setStretchFactor 桩的句柄 */
function fakeChart() {
  const seriesApis: Array<{ setData: ReturnType<typeof vi.fn> }> = []
  const paneApis: Array<{ paneIndex: () => number; setStretchFactor: ReturnType<typeof vi.fn> }> = []
  const calls = {
    addSeries: [] as Array<{ def: unknown; paneIdx: number | undefined }>,
    removedSeries: [] as unknown[],
    addPane: 0,
    removedPanes: [] as number[],
    appliedHeights: [] as Array<number | undefined>,
  }
  const chart = {
    addSeries: (def: unknown, _opts: unknown, paneIdx?: number) => {
      calls.addSeries.push({ def, paneIdx })
      const api = { setData: vi.fn() }
      seriesApis.push(api)
      return api
    },
    removeSeries: (api: unknown) => {
      calls.removedSeries.push(api)
    },
    addPane: () => {
      calls.addPane += 1
      const idx = paneApis.length + 1
      const pane = { paneIndex: () => idx, setStretchFactor: vi.fn() }
      paneApis.push(pane)
      return pane
    },
    panes: () => [{}, ...paneApis], // 索引 0 为主图
    removePane: (i: number) => {
      calls.removedPanes.push(i)
      paneApis.splice(i - 1, 1)
    },
    applyOptions: (o: { height?: number }) => {
      calls.appliedHeights.push(o.height)
    },
  }
  return { chart: chart as unknown as IChartApi, calls, seriesApis, paneApis }
}

function pt(time: number, value: number) {
  return { time: time as UTCTimestamp, value }
}

function overlay(id: string, value = 1): OverlayLine {
  const [key, field] = id.split('.')
  return { id, key, field, color: '#ffffff', data: [pt(100, value)] }
}

function paneLine(field: string, histogram = false, value = 1): PaneLine {
  return { field, color: '#ffffff', histogram, data: [pt(100, value)] }
}

const RSI: PaneSpec = { key: 'rsi14', lines: [paneLine('rsi14')] }
const MACD: PaneSpec = {
  key: 'macd',
  lines: [paneLine('dif'), paneLine('dea'), paneLine('hist', true)],
}
const KDJ: PaneSpec = { key: 'kdj', lines: [paneLine('k'), paneLine('d'), paneLine('j')] }

describe('useIndicatorChart', () => {
  it('初次挂接：overlay 挂主图（无 paneIdx），pane 指标各建副图，高度随副图数量调整', () => {
    const { chart, calls, seriesApis, paneApis } = fakeChart()
    const overlays = [overlay('ema20.ema20'), overlay('ema50.ema50')]
    renderHook(() => useIndicatorChart(chart, overlays, [RSI, MACD]))

    // 6 条系列：2 overlay（主图）+ rsi14（副图1）+ macd 3 条（副图2，hist 用 Histogram）
    expect(calls.addSeries).toHaveLength(6)
    expect(calls.addSeries[0]).toEqual({ def: LineSeries, paneIdx: undefined })
    expect(calls.addSeries[1]).toEqual({ def: LineSeries, paneIdx: undefined })
    expect(calls.addSeries[2]).toEqual({ def: LineSeries, paneIdx: 1 })
    expect(calls.addSeries[3]).toEqual({ def: LineSeries, paneIdx: 2 })
    expect(calls.addSeries[4]).toEqual({ def: LineSeries, paneIdx: 2 })
    expect(calls.addSeries[5]).toEqual({ def: HistogramSeries, paneIdx: 2 })
    expect(calls.addPane).toBe(2)
    for (const pane of paneApis) expect(pane.setStretchFactor).toHaveBeenCalled()
    for (const api of seriesApis) expect(api.setData).toHaveBeenCalledTimes(1)
    // 高度 = 主图 300 + 2 副图 × 110
    expect(calls.appliedHeights.at(-1)).toBe(520)
  })

  it('pane 签名不变时仅刷数据：不新增/移除系列与副图', () => {
    const { chart, calls, seriesApis } = fakeChart()
    const overlays = [overlay('ema20.ema20')]
    const { rerender } = renderHook(
      (p: { overlays: OverlayLine[]; panes: PaneSpec[] }) => useIndicatorChart(chart, p.overlays, p.panes),
      { initialProps: { overlays, panes: [RSI] } },
    )
    expect(calls.addSeries).toHaveLength(2)

    rerender({
      overlays: [overlay('ema20.ema20', 2)],
      panes: [{ key: 'rsi14', lines: [paneLine('rsi14', false, 60)] }],
    })
    expect(calls.addSeries).toHaveLength(2) // 未新增
    expect(calls.removedSeries).toHaveLength(0)
    expect(calls.removedPanes).toHaveLength(0)
    for (const api of seriesApis) expect(api.setData).toHaveBeenCalledTimes(2)
    expect(calls.appliedHeights.at(-1)).toBe(410)
  })

  it('pane 集合变化整体重建：旧系列移除、副图自高向低摘除、按新配置重建', () => {
    const { chart, calls } = fakeChart()
    const { rerender } = renderHook(
      (p: { overlays: OverlayLine[]; panes: PaneSpec[] }) => useIndicatorChart(chart, p.overlays, p.panes),
      { initialProps: { overlays: [] as OverlayLine[], panes: [RSI, MACD] } },
    )
    expect(calls.addSeries).toHaveLength(4) // rsi1 + macd3

    rerender({ overlays: [], panes: [KDJ] })
    expect(calls.removedSeries).toHaveLength(4) // 旧副图系列全部移除
    expect(calls.removedPanes).toEqual([2, 1]) // 自高索引向低摘除，避免索引位移误删
    expect(calls.addPane).toBe(3)
    expect(calls.addSeries).toHaveLength(7) // + kdj3
    expect(calls.appliedHeights.at(-1)).toBe(410)
  })

  it('短名单收缩：消失的 overlay 系列被移除，pane 清空时高度回落 300', () => {
    const { chart, calls } = fakeChart()
    const overlays = [overlay('ema20.ema20'), overlay('ema50.ema50')]
    const { rerender } = renderHook(
      (p: { overlays: OverlayLine[]; panes: PaneSpec[] }) => useIndicatorChart(chart, p.overlays, p.panes),
      { initialProps: { overlays, panes: [RSI] } },
    )

    rerender({ overlays: [overlay('ema20.ema20')], panes: [] })
    expect(calls.removedSeries).toHaveLength(2) // ema50 + rsi14
    expect(calls.removedPanes).toEqual([1])
    expect(calls.appliedHeights.at(-1)).toBe(300)
  })

  it('chart 为 null 时不动作', () => {
    const { calls } = fakeChart()
    renderHook(() => useIndicatorChart(null, [overlay('ema20.ema20')], [RSI]))
    expect(calls.addSeries).toHaveLength(0)
    expect(calls.addPane).toBe(0)
  })
})
