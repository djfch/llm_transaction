/**
 * 指标图表挂接 hook：overlay 线挂主图（默认 price 轴），pane 指标经 chart.addPane() 建副图挂系列。
 * overlay 按 id 增量同步（消失的移除 / 新增的创建 / 其余 setData）；
 * pane 因 removePane 会引起索引位移，key 集合（顺序签名）变化时整体重建，仅数据变化时逐系列 setData；
 * 图表实例变化（组件重挂载）时句柄全部重置（旧实例已由 KlinePanel chart.remove() 销毁）。
 */
import { useEffect, useRef } from 'react'
import {
  HistogramSeries,
  LineSeries,
  type IChartApi,
  type ISeriesApi,
} from 'lightweight-charts'
import { INDICATOR_LINE_WIDTH, type OverlayLine, type PaneSpec } from '../utils/indicatorSeries'

/** 主图基础高度（与 createKlineChart 一致）；每个副图额外增加的图表高度 */
const BASE_CHART_HEIGHT = 300
const PANE_CHART_HEIGHT = 110
/** 副图伸缩因子：主图为 1 时副图约其 0.55 高，保持主图主体地位 */
const PANE_STRETCH = 0.55

type OverlayApi = ISeriesApi<'Line'>
type PaneSeriesApi = ISeriesApi<'Line'> | ISeriesApi<'Histogram'>

/** 单个副图的已挂系列句柄（field → 系列），顺序与图表 paneIndex 1..n 对应 */
interface PaneState {
  key: string
  series: Map<string, PaneSeriesApi>
}

/** 指标线公共选项：细线、不画价格线/末值标签/十字标记（不抢蜡烛主体，与 MA20 风格一致） */
function lineOptions(color: string) {
  return {
    color,
    lineWidth: INDICATOR_LINE_WIDTH,
    priceLineVisible: false,
    lastValueVisible: false,
    crosshairMarkerVisible: false,
  } as const
}

/** overlay 增量同步：消失的系列移除，新增的创建，已存在的仅刷数据 */
function syncOverlays(chart: IChartApi, apis: Map<string, OverlayApi>, overlays: OverlayLine[]): void {
  const want = new Map(overlays.map((line) => [line.id, line]))
  for (const [id, api] of apis) {
    if (!want.has(id)) {
      chart.removeSeries(api)
      apis.delete(id)
    }
  }
  for (const [id, line] of want) {
    let api = apis.get(id)
    if (!api) {
      api = chart.addSeries(LineSeries, lineOptions(line.color))
      apis.set(id, api)
    }
    api.setData(line.data)
  }
}

/** 移除全部副图：先摘系列再自高索引向低摘除（removePane 会引起后续 pane 索引位移） */
function removeAllPanes(chart: IChartApi, states: PaneState[]): void {
  for (const state of states) {
    for (const api of state.series.values()) chart.removeSeries(api)
  }
  for (let i = chart.panes().length - 1; i >= 1; i -= 1) chart.removePane(i)
}

/** 按配置重建副图：每个 pane 指标一个 addPane()，hist 字段挂 Histogram、其余挂 Line */
function buildPanes(chart: IChartApi, panes: PaneSpec[]): PaneState[] {
  return panes.map((spec) => {
    const pane = chart.addPane()
    pane.setStretchFactor(PANE_STRETCH)
    const idx = pane.paneIndex()
    const series = new Map<string, PaneSeriesApi>()
    for (const line of spec.lines) {
      const api: PaneSeriesApi = line.histogram
        ? chart.addSeries(HistogramSeries, { priceLineVisible: false, lastValueVisible: false }, idx)
        : chart.addSeries(LineSeries, lineOptions(line.color), idx)
      api.setData(line.data)
      series.set(line.field, api)
    }
    return { key: spec.key, series }
  })
}

export function useIndicatorChart(chart: IChartApi | null, overlays: OverlayLine[], panes: PaneSpec[]): void {
  const overlayApisRef = useRef<Map<string, OverlayApi>>(new Map())
  const paneStatesRef = useRef<PaneState[]>([])
  const paneSigRef = useRef('')

  // 图表实例变化：旧实例上的系列/pane 句柄随之失效，全部重置
  useEffect(() => {
    overlayApisRef.current = new Map()
    paneStatesRef.current = []
    paneSigRef.current = ''
  }, [chart])

  // overlay/pane 同步：pane key 签名变化整体重建，否则仅刷数据；图表高度随副图数量调整
  useEffect(() => {
    if (!chart) return
    syncOverlays(chart, overlayApisRef.current, overlays)
    const sig = panes.map((p) => p.key).join(',')
    if (sig === paneSigRef.current) {
      panes.forEach((spec, i) => {
        const state = paneStatesRef.current[i]
        spec.lines.forEach((line) => state?.series.get(line.field)?.setData(line.data))
      })
    } else {
      removeAllPanes(chart, paneStatesRef.current)
      paneStatesRef.current = buildPanes(chart, panes)
      paneSigRef.current = sig
    }
    chart.applyOptions({ height: BASE_CHART_HEIGHT + panes.length * PANE_CHART_HEIGHT })
  }, [chart, overlays, panes])
}
