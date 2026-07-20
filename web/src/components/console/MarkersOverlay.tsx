/**
 * K线买卖点覆盖层：把当前合约已归属决策轮的成交渲染为可点击的 b/s 标记。
 * 定位：成交时间按周期分桶到所属 bar，x = timeScale().timeToCoordinate(bar 开盘时刻)，
 * y = series.priceToCoordinate(bar low/high)（买 b 置 K线下方/绿，卖 s 置 K线上方/红）；
 * 坐标为 null（滚出可视范围）时隐藏。拖拽/缩放与容器 resize 都会触发重定位。
 * 点击标记 → useRoundFocus().focus(roundId) 定位决策轮。
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

/** 已换算坐标的标记（null 坐标已在换算层剔除） */
interface PositionedMarker extends TradeMarker {
  x: number
  y: number
}

interface MarkersOverlayProps {
  chart: IChartApi | null // 图表实例（未就绪时不渲染）
  series: ISeriesApi<'Candlestick'> | null
  bars: Candle[] // 当前 K 线数据（标记锚定到所属 bar 的 low/high）
  contract: string // 当前选中合约（空串不取数）
  intervalSec: number // 当前周期秒数（成交归属 bar 的分桶依据）
}

/** 拉最近 100 笔成交并过滤出当前合约标记（换合约重新取数；WS round 事件后重拉刷新标记） */
function useTradeMarkers(contract: string): TradeMarker[] {
  const query = useApiData(
    () => (contract ? api.getTrades(0, 100) : Promise.resolve(null)),
    [contract],
  )
  // round 事件 = 失效信号：新轮成交需及时上图（与 TradesTable 同一订阅模式）
  const { lastMessage } = useWs()
  const { reload } = query
  useEffect(() => {
    if (lastMessage?.type === 'round') reload()
  }, [lastMessage, reload])
  return useMemo(
    () => buildTradeMarkers(query.data?.items ?? [], contract),
    [query.data, contract],
  )
}

/** 标记圆点样式：b 绿 / s 红；容器整体 pointer-events-none，仅标记自身可点 */
function markerClass(side: 'buy' | 'sell'): string {
  const base =
    'pointer-events-auto absolute z-10 flex h-4 w-4 -translate-x-1/2 -translate-y-1/2 items-center justify-center rounded-full border font-mono text-[10px] font-bold leading-none transition hover:scale-125'
  return side === 'buy'
    ? `${base} border-emerald-300/70 bg-emerald-500/25 text-emerald-300`
    : `${base} border-rose-400/70 bg-rose-500/25 text-rose-300`
}

/** 全部标记换算坐标；timeToCoordinate/priceToCoordinate 返回 null（超出可视范围）则跳过 */
function positionMarkers(
  chart: IChartApi,
  series: ISeriesApi<'Candlestick'>,
  markers: TradeMarker[],
  barByTime: Map<number, Candle>,
  intervalSec: number,
): PositionedMarker[] {
  const ts = chart.timeScale()
  const out: PositionedMarker[] = []
  for (const m of markers) {
    const bucket = barStart(m.timeSec, intervalSec)
    const x = ts.timeToCoordinate(bucket as UTCTimestamp)
    if (x === null) continue
    const bar = barByTime.get(bucket)
    const anchor = bar ? (m.side === 'buy' ? bar.l : bar.h) : m.price
    const y = series.priceToCoordinate(anchor)
    if (y === null) continue
    // 买置于 K 线（low）下方，卖置于（high）上方，各留 16px 间距
    out.push({ ...m, x, y: y + (m.side === 'buy' ? 16 : -16) })
  }
  return out
}

export default function MarkersOverlay({ chart, series, bars, contract, intervalSec }: MarkersOverlayProps) {
  const { focus } = useRoundFocus()
  const markers = useTradeMarkers(contract)
  const [positioned, setPositioned] = useState<PositionedMarker[]>([])
  const rootRef = useRef<HTMLDivElement>(null)

  // 重定位：图表/数据/合约/周期变化 + 拖拽缩放（subscribeVisibleLogicalRangeChange）+ 容器 resize
  useEffect(() => {
    if (!chart || !series) return
    const barByTime = new Map(bars.map((b) => [b.t, b]))
    const relayout = () => setPositioned(positionMarkers(chart, series, markers, barByTime, intervalSec))
    relayout()
    const ts = chart.timeScale()
    ts.subscribeVisibleLogicalRangeChange(relayout)
    const el = rootRef.current
    const observer = new ResizeObserver(relayout)
    if (el) observer.observe(el)
    return () => {
      ts.unsubscribeVisibleLogicalRangeChange(relayout)
      observer.disconnect()
    }
  }, [chart, series, bars, markers, intervalSec])

  if (!chart || !series) return null
  return (
    <div ref={rootRef} className="pointer-events-none absolute inset-0" data-testid="markers-overlay">
      {positioned.map((m) => (
        <button
          key={m.id}
          type="button"
          title={`${m.side === 'buy' ? '买入/开多' : '卖出/平多'} @ ${m.price} · 点击定位决策轮 ${m.roundId}`}
          onClick={() => focus(m.roundId)}
          className={markerClass(m.side)}
          style={{ left: m.x, top: m.y }}
        >
          {m.side === 'buy' ? 'b' : 's'}
        </button>
      ))}
    </div>
  )
}
