/**
 * K线面板：合约（watchlist 驱动）+ 周期（15m/1h/4h/1d，默认 1h）切换的蜡烛图。
 * 三个序列：蜡烛 + 成交量 histogram（隐形刻度叠加主图下部，涨绿跌红半透明）+ MA20（青色细线，
 * 客户端计算，前 19 根无值不画）；周期/合约切换时经同一数据 effect 全部重建。
 * 历史加载 15m:700/1h:200/4h:100/1d:60 根，切换后 timeScale 定位最近约 48 根（拖拽/滚轮走库原生）。
 * WS ticker 驱动末根实时跳动（mergeTick 合成）+ 末根 MA20 实时延伸（liveMaValue）；
 * 成交量无 ticker 数据源，histogram 不随 tick 更新（待下次数据重建刷新）。
 * 表头涨跌幅为真 24h 口径（changePct24h，与图表同源的 sortedUnique memo）；买卖点标记由
 * 圆形覆盖层渲染买卖点（点击定位决策轮，移出主价格绘图区时隐藏）。
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import {
  CandlestickSeries,
  ColorType,
  createChart,
  HistogramSeries,
  LineSeries,
  type IChartApi,
  type ISeriesApi,
  type LogicalRange,
  type UTCTimestamp,
} from 'lightweight-charts'
import { api } from '../../api'
import { useApiData } from '../../hooks/useApiData'
import { useWs } from '../../hooks/useWs'
import { barStart, intervalToSec, mergeTick, type LiveBar } from '../../utils/candleLive'
import { fmtPrice, fmtSignedPct } from '../../utils/format'
import { changePct24h, liveMaValue, ma20Points, sortedUnique, toCandlePoint, toVolumePoint } from '../../utils/klineStats'
import MarkersOverlay from './MarkersOverlay'

/** 可选周期 */
const INTERVALS = ['15m', '1h', '4h', '1d'] as const
type IntervalKey = (typeof INTERVALS)[number]
/** 各周期历史加载根数（15m 高密度用于拖拽回看） */
const INTERVAL_LIMIT: Record<IntervalKey, number> = { '15m': 700, '1h': 200, '4h': 100, '1d': 60 }
/** 切换数据后可视窗口定位：最近约 48 根 */
const VISIBLE_BARS = 48

const selectClass =
  'rounded-lg border border-zinc-700 bg-zinc-900 px-2 py-1.5 text-xs text-zinc-200 focus:border-violet-400 focus:outline-none'

/** 周期切换按钮样式（选中高亮紫） */
function intervalBtnClass(active: boolean): string {
  const base = 'rounded px-2 py-1 font-mono text-[11px] transition'
  return active
    ? `${base} border border-violet-400/50 bg-violet-400/10 text-violet-300`
    : `${base} border border-transparent text-zinc-500 hover:text-zinc-300`
}

/** 创建图表与三个序列（蜡烛 + 成交量副轴 + MA20），返回句柄供挂载 effect 装配 */
function createKlineChart(el: HTMLDivElement) {
  const chart = createChart(el, {
    height: 300,
    layout: { background: { type: ColorType.Solid, color: 'transparent' }, textColor: '#a1a1aa', fontSize: 11 },
    grid: { vertLines: { color: '#27272a' }, horzLines: { color: '#27272a' } },
    rightPriceScale: { borderColor: '#3f3f46' },
    timeScale: { borderColor: '#3f3f46', timeVisible: true, secondsVisible: false },
  })
  const upDown = { upColor: '#34d399', downColor: '#fb7185', wickUpColor: '#34d399', wickDownColor: '#fb7185' }
  const candle = chart.addSeries(CandlestickSeries, { ...upDown, borderVisible: false })
  // 成交量：priceScaleId ''（隐形刻度）叠加在主图下部，主价格轴上移让位
  const volume = chart.addSeries(HistogramSeries, {
    priceFormat: { type: 'volume' }, priceScaleId: '', lastValueVisible: false, priceLineVisible: false,
  })
  chart.priceScale('').applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } })
  chart.priceScale('right').applyOptions({ scaleMargins: { top: 0.08, bottom: 0.26 } })
  // MA20：青色细线，不抢蜡烛主体
  const ma = chart.addSeries(LineSeries, {
    color: '#22d3ee', lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
  })
  return { chart, candle, volume, ma }
}

export default function KlinePanel() {
  const chartBoxRef = useRef<HTMLDivElement>(null)
  const [chartApi, setChartApi] = useState<IChartApi | null>(null)
  const [seriesApi, setSeriesApi] = useState<ISeriesApi<'Candlestick'> | null>(null)
  const [volumeApi, setVolumeApi] = useState<ISeriesApi<'Histogram'> | null>(null)
  const [maApi, setMaApi] = useState<ISeriesApi<'Line'> | null>(null)
  // 实时合成中的当前 bar（每次 setData 后重置为数据末根；切合约/周期立即清空防串数据）
  const currentBarRef = useRef<LiveBar | null>(null)
  // 已完成 bar 的收盘价（不含 live bar），末根 MA20 实时延伸用
  const historyRef = useRef<number[]>([])

  const watchlistQ = useApiData(() => api.getWatchlist(), [])
  const [contract, setContract] = useState('')
  const [interval, setInterval] = useState<IntervalKey>('1h')
  // useMemo 固定引用，避免每次渲染都生成新数组触发 effect 告警
  const contracts = useMemo(() => watchlistQ.data?.contracts ?? [], [watchlistQ.data])
  const intervalSec = intervalToSec(interval) ?? 3600

  // 白名单加载完成后默认选中第一个合约
  useEffect(() => {
    if (!contract && contracts.length > 0) setContract(contracts[0])
  }, [contracts, contract])

  const candlesQ = useApiData(
    () => (contract ? api.getCandles(contract, interval, INTERVAL_LIMIT[interval]) : Promise.resolve([])),
    [contract, interval],
  )
  const bars = useMemo(() => candlesQ.data ?? [], [candlesQ.data])
  // 排序去重后的唯一数据源：图表重建、24h 涨跌幅、实时延伸历史共用同一 memo
  const sortedBars = useMemo(() => sortedUnique(bars), [bars])

  // 表头涨跌幅（0-1 比例）：真 24h 口径（基准=最后一根 t ≤ t_last−86400 的 bar）；
  // 窗口不足/基准为 0 → null 不显示
  const dayChangePct = useMemo(() => changePct24h(sortedBars), [sortedBars])

  // 切合约/周期：立即清空实时合成状态（新数据到达前 tick 一律跳过，防旧 bar 串入新序列）
  useEffect(() => {
    currentBarRef.current = null
    historyRef.current = []
  }, [contract, interval])

  // 挂载时创建图表（蜡烛 + 成交量 + MA20），卸载时销毁（handleScroll/handleScale 默认开）
  useEffect(() => {
    const el = chartBoxRef.current
    if (!el) return
    const h = createKlineChart(el)
    setChartApi(h.chart)
    setSeriesApi(h.candle)
    setVolumeApi(h.volume)
    setMaApi(h.ma)
    const observer = new ResizeObserver((entries) => {
      h.chart.applyOptions({ width: Math.floor(entries[0].contentRect.width) })
    })
    observer.observe(el)
    return () => {
      observer.disconnect()
      h.chart.remove()
      setChartApi(null)
      setSeriesApi(null)
      setVolumeApi(null)
      setMaApi(null)
    }
  }, [])

  // K 线数据更新时三个序列整体重建，并定位最近约 48 根
  useEffect(() => {
    if (!seriesApi || !chartApi) return
    const data = sortedBars
    seriesApi.setData(data.map(toCandlePoint))
    volumeApi?.setData(data.map(toVolumePoint))
    maApi?.setData(ma20Points(data))
    // 实时合成基准重置为新数据末根；历史收盘 = 末根之前的全部收盘（MA 实时延伸用）
    const last = data[data.length - 1]
    historyRef.current = data.slice(0, -1).map((b) => b.c)
    currentBarRef.current = last
      ? { time: last.t, open: last.o, high: last.h, low: last.l, close: last.c }
      : null
    if (data.length > 0) {
      const range = { from: Math.max(0, data.length - VISIBLE_BARS), to: data.length - 1 }
      chartApi.timeScale().setVisibleLogicalRange(range as LogicalRange)
    }
  }, [sortedBars, seriesApi, volumeApi, maApi, chartApi])

  // WS ticker 推送 → 实时最新价（只收当前选中合约，渲染守卫兜底防串合约）
  const { lastMessage } = useWs()
  const [live, setLive] = useState<{ contract: string; last: number } | null>(null)
  useEffect(() => {
    if (lastMessage?.type === 'ticker' && lastMessage.data.contract === contract) {
      setLive(lastMessage.data)
    }
  }, [lastMessage, contract])
  const livePrice = live && live.contract === contract ? live.last : null

  // 最新价 → 末根 bar 实时跳动（合并语义见 candleLive 单测）；末根 MA20 同步延伸。
  // 成交量无 ticker 数据源，histogram 不随 tick 更新，待下次数据重建刷新。
  useEffect(() => {
    if (!seriesApi || livePrice == null) return
    const prev = currentBarRef.current
    if (!prev) return // 数据未就绪或切换合约/周期中（已清空），跳过 merge 防串数据
    const barTime = barStart(Math.floor(Date.now() / 1000), intervalSec)
    const bar = mergeTick(prev, livePrice, barTime)
    if (!bar) return // 迟到的旧周期 tick
    if (bar.time > prev.time) historyRef.current.push(prev.close) // 跨 bar：完成的旧 bar 收盘入历史
    currentBarRef.current = bar
    seriesApi.update({
      time: bar.time as UTCTimestamp,
      open: bar.open,
      high: bar.high,
      low: bar.low,
      close: bar.close,
    })
    // 末根 MA20 实时延伸：最近 19 根历史收盘 + live close（不足 19 根跳过）
    const maValue = liveMaValue(historyRef.current, bar.close)
    if (maValue !== null) maApi?.update({ time: bar.time as UTCTimestamp, value: maValue })
  }, [livePrice, intervalSec, seriesApi, maApi])

  // watchlist 失败要透出错误（否则 contract 永远为空、面板永久"加载中"）
  if (watchlistQ.error) {
    return (
      <section className="rounded-xl border border-zinc-800 bg-zinc-950/80 p-4 shadow-lg shadow-black/30">
        <h2 className="text-sm font-semibold text-zinc-200">K线</h2>
        <p className="py-8 text-center text-sm text-rose-400">加载失败：{watchlistQ.error}</p>
      </section>
    )
  }

  return (
    <section className="rounded-xl border border-zinc-800 bg-zinc-950/80 p-4 shadow-lg shadow-black/30">
      <header className="flex flex-wrap items-center gap-2">
        <h2 className="text-sm font-semibold text-zinc-200">K线</h2>
        {livePrice != null && (
          <span data-testid="kline-live-price" className="font-mono text-xs tabular-nums text-cyan-300">
            {fmtPrice(livePrice)}
            <span className="ml-1 text-[10px] text-zinc-600">· WS实时</span>
          </span>
        )}
        {dayChangePct !== null && (
          <span
            data-testid="kline-day-change"
            className={`font-mono text-[11px] tabular-nums ${dayChangePct >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}
          >
            {fmtSignedPct(dayChangePct)}
            <span className="ml-1 text-[10px] text-zinc-600">· 24h</span>
          </span>
        )}
        <div className="ml-auto flex items-center gap-2">
          <label className="flex items-center gap-1.5 text-xs text-zinc-500">
            合约
            <select
              value={contract}
              onChange={(e) => setContract(e.target.value)}
              className={selectClass}
            >
              {contracts.map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </select>
          </label>
          <div className="flex items-center gap-0.5" role="group" aria-label="周期">
            {INTERVALS.map((key) => (
              <button
                key={key}
                type="button"
                aria-pressed={interval === key}
                onClick={() => setInterval(key)}
                className={intervalBtnClass(interval === key)}
              >
                {key}
              </button>
            ))}
          </div>
        </div>
      </header>

      <div className="relative mt-3">
        <div ref={chartBoxRef} className="w-full" data-testid="kline-chart" />
        <MarkersOverlay
          chart={chartApi}
          series={seriesApi}
          bars={sortedBars}
          contract={contract}
          intervalSec={intervalSec}
        />
        {candlesQ.loading && (
          <div className="absolute inset-0 flex items-center justify-center bg-zinc-950/60 text-xs text-zinc-500">
            加载中…
          </div>
        )}
        {!candlesQ.loading && candlesQ.error && (
          <div className="absolute inset-0 flex items-center justify-center bg-zinc-950/60 text-xs text-rose-400">
            加载失败：{candlesQ.error}
          </div>
        )}
        {!candlesQ.loading && !candlesQ.error && contract !== '' && bars.length === 0 && (
          <div className="absolute inset-0 flex items-center justify-center text-xs text-zinc-500">
            暂无数据
          </div>
        )}
      </div>

      <footer className="mt-1.5 flex flex-wrap items-center gap-2 text-[10px] text-zinc-600">
        <span>
          <span className="font-mono font-bold text-emerald-400">b</span> 买入/开多 ·{' '}
          <span className="font-mono font-bold text-rose-400">s</span> 卖出/平多（点击标记跳转决策轮）
        </span>
        <span className="ml-auto font-mono">⟷ 拖拽 / 滚轮平移</span>
      </footer>
    </section>
  )
}
