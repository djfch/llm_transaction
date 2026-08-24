/**
 * K线买卖点覆盖层：保留原圆形 b/s 徽标，锚点不变（卖在 K 线高点上方、买在低点下方）。
 * 坐标随图表可视范围重算；仅当整个标记圆越出主图面板边界（时间轴两端或面板上下沿，
 * 含顶部留白与成交量带）时隐藏——标记允许落在主价格绘图区之外的面板留白里，
 * 用户缩小/拖动图表使其回到面板内时重新显示；不影响图表纵轴自动缩放。
 * 数据：当前合约成交（getTrades 带合约过滤）；WS trades_updated 事件触发重拉；
 * 无归属决策轮（roundId 空串）的标记照常绘制但渲染为不可点击。
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import type { IChartApi, ISeriesApi, UTCTimestamp } from 'lightweight-charts'
import { api } from '../../api'
import type { Candle } from '../../api/types'
import { useApiData } from '../../hooks/useApiData'
import { useRoundFocus } from '../../hooks/useRoundFocus'
import { useWs } from '../../hooks/useWs'
import { barStart } from '../../utils/candleLive'
import { buildTradeMarkers, type TradeMarker } from '../../utils/klineMarkers'

const MARKER_SIZE = 16
const MARKER_OFFSET = 16

interface PositionedMarker extends TradeMarker {
  x: number
  y: number
}

interface MarkersOverlayProps {
  chart: IChartApi | null
  series: ISeriesApi<'Candlestick'> | null
  bars: Candle[]
  contract: string
  intervalSec: number
}

function useTradeMarkers(contract: string): TradeMarker[] {
  const query = useApiData(
    () => (contract ? api.getTrades(0, 100, contract) : Promise.resolve(null)),
    [contract],
  )
  // WS trades_updated 事件：仅作失效信号，重拉当前合约的成交标记
  const { lastMessage } = useWs()
  const { reload } = query
  useEffect(() => {
    if (lastMessage?.type === 'trades_updated') reload()
  }, [lastMessage, reload])
  return useMemo(
    () => buildTradeMarkers(query.data?.items ?? [], contract),
    [query.data, contract],
  )
}

/** 标记样式：interactive=false（无归属决策轮）时无 hover 放大、不可点击 */
function markerClass(side: 'buy' | 'sell', interactive: boolean): string {
  const base =
    'pointer-events-auto absolute z-10 flex h-4 w-4 -translate-x-1/2 -translate-y-1/2 items-center justify-center rounded-full border font-mono text-[10px] font-bold leading-none'
  const inter = interactive ? ' transition hover:scale-125' : ''
  return side === 'buy'
    ? `${base}${inter} border-emerald-300/70 bg-emerald-500/25 text-emerald-300`
    : `${base}${inter} border-rose-400/70 bg-rose-500/25 text-rose-300`
}

/** 标记圆是否完整落在主图面板内：越出时间轴两端或面板上下沿即整体隐藏（进入留白/成交量带不隐藏） */
function markerInsidePane(x: number, y: number, width: number, height: number): boolean {
  const radius = MARKER_SIZE / 2
  return x >= radius && x <= width - radius && y >= radius && y <= height - radius
}

function positionMarkers(
  chart: IChartApi,
  series: ISeriesApi<'Candlestick'>,
  markers: TradeMarker[],
  barByTime: Map<number, Candle>,
  intervalSec: number,
  plotWidth: number,
  paneHeight: number,
): PositionedMarker[] {
  if (plotWidth <= 0 || paneHeight <= 0) return []
  const timeScale = chart.timeScale()
  const positioned: PositionedMarker[] = []

  for (const marker of markers) {
    const bucket = barStart(marker.timeSec, intervalSec)
    const x = timeScale.timeToCoordinate(bucket as UTCTimestamp)
    if (x === null) continue
    const bar = barByTime.get(bucket)
    const anchor = bar ? (marker.side === 'buy' ? bar.l : bar.h) : marker.price
    const priceY = series.priceToCoordinate(anchor)
    if (priceY === null) continue
    const y = priceY + (marker.side === 'buy' ? MARKER_OFFSET : -MARKER_OFFSET)
    if (!markerInsidePane(x, y, plotWidth, paneHeight)) continue
    positioned.push({ ...marker, x, y })
  }
  return positioned
}

export default function MarkersOverlay({
  chart,
  series,
  bars,
  contract,
  intervalSec,
}: MarkersOverlayProps) {
  const { focus } = useRoundFocus()
  const markers = useTradeMarkers(contract)
  const [positioned, setPositioned] = useState<PositionedMarker[]>([])
  const rootRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const root = rootRef.current
    if (!chart || !series || !root) return
    const barByTime = new Map(bars.map((bar) => [bar.t, bar]))
    const timeScale = chart.timeScale()
    let frame: number | null = null
    const relayout = () => {
      setPositioned(
        positionMarkers(
          chart,
          series,
          markers,
          barByTime,
          intervalSec,
          timeScale.width(),
          series.getPane().getHeight(),
        ),
      )
    }
    const scheduleRelayout = () => {
      if (frame !== null) cancelAnimationFrame(frame)
      frame = requestAnimationFrame(() => {
        frame = null
        relayout()
      })
    }
    relayout()
    timeScale.subscribeVisibleLogicalRangeChange(scheduleRelayout)
    timeScale.subscribeSizeChange(scheduleRelayout)
    series.subscribeDataChanged(scheduleRelayout)
    const observer = new ResizeObserver(scheduleRelayout)
    observer.observe(root)
    const host = root.parentElement
    const interactionEvents = ['pointermove', 'pointerup', 'wheel', 'dblclick'] as const
    interactionEvents.forEach((event) =>
      host?.addEventListener(event, scheduleRelayout, { passive: true }),
    )
    return () => {
      if (frame !== null) cancelAnimationFrame(frame)
      timeScale.unsubscribeVisibleLogicalRangeChange(scheduleRelayout)
      timeScale.unsubscribeSizeChange(scheduleRelayout)
      series.unsubscribeDataChanged(scheduleRelayout)
      observer.disconnect()
      interactionEvents.forEach((event) =>
        host?.removeEventListener(event, scheduleRelayout),
      )
    }
  }, [chart, series, bars, markers, intervalSec])

  if (!chart || !series) return null
  return (
    <div
      ref={rootRef}
      className="pointer-events-none absolute inset-0 overflow-hidden"
      data-testid="markers-overlay"
    >
      {positioned.map((marker) => {
        const action = marker.side === 'buy' ? '买入成交' : '卖出成交'
        const clickable = marker.roundId !== ''
        const label = clickable
          ? `${action} @ ${marker.price} · 点击定位决策轮 ${marker.roundId}`
          : `${action} @ ${marker.price} · 无归属决策轮`
        // 无归属决策轮的成交（历史/强平/止盈止损等）：照常绘制但不可点击
        if (!clickable) {
          return (
            <span
              key={marker.id}
              aria-label={label}
              title={label}
              className={markerClass(marker.side, false)}
              style={{ left: marker.x, top: marker.y }}
            >
              {marker.side === 'buy' ? 'b' : 's'}
            </span>
          )
        }
        return (
          <button
            key={marker.id}
            type="button"
            aria-label={label}
            title={label}
            onClick={() => focus(marker.roundId)}
            className={markerClass(marker.side, true)}
            style={{ left: marker.x, top: marker.y }}
          >
            {marker.side === 'buy' ? 'b' : 's'}
          </button>
        )
      })}
    </div>
  )
}
