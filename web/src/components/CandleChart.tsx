/**
 * K 线图：lightweight-charts v5 CandlestickSeries 封装（深色主题与 EquityChart 一致，自适应宽度）。
 * livePrice + intervalSec 可选：传入后最后一根 bar 随 WS 最新价实时跳动（mergeTick 纯函数合成）。
 */
import { useEffect, useRef } from 'react'
import {
  CandlestickSeries,
  ColorType,
  createChart,
  type IChartApi,
  type ISeriesApi,
  type UTCTimestamp,
} from 'lightweight-charts'
import type { Candle } from '../api/types'
import { barStart, mergeTick, type LiveBar } from '../utils/candleLive'

export default function CandleChart({
  data,
  livePrice = null,
  intervalSec = null,
}: {
  data: Candle[]
  livePrice?: number | null // WS 实时最新价（不传则纯静态图）
  intervalSec?: number | null // 当前周期秒数（bar 分桶依据）
}) {
  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const seriesRef = useRef<ISeriesApi<'Candlestick'> | null>(null)
  // 实时合成中的当前 bar（每次 setData 后重置为数据末根）
  const currentBarRef = useRef<LiveBar | null>(null)

  // 挂载时创建图表，卸载时销毁
  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    const chart = createChart(el, {
      height: 280,
      layout: {
        background: { type: ColorType.Solid, color: 'transparent' },
        textColor: '#94a3b8',
        fontSize: 12,
      },
      grid: {
        vertLines: { color: '#1e293b' },
        horzLines: { color: '#1e293b' },
      },
      rightPriceScale: { borderColor: '#334155' },
      timeScale: { borderColor: '#334155', timeVisible: true, secondsVisible: false },
    })
    const series = chart.addSeries(CandlestickSeries, {
      upColor: '#10b981',
      downColor: '#f43f5e',
      wickUpColor: '#10b981',
      wickDownColor: '#f43f5e',
      borderVisible: false,
    })
    chartRef.current = chart
    seriesRef.current = series

    // 容器尺寸变化时自适应
    const observer = new ResizeObserver((entries) => {
      const { width } = entries[0].contentRect
      chart.applyOptions({ width: Math.floor(width) })
    })
    observer.observe(el)

    return () => {
      observer.disconnect()
      chart.remove()
      chartRef.current = null
      seriesRef.current = null
    }
  }, [])

  // 数据更新时刷新序列
  useEffect(() => {
    const series = seriesRef.current
    if (!series || data.length === 0) return
    const bars = data
      .map((k) => ({
        time: k.t as UTCTimestamp,
        open: k.o,
        high: k.h,
        low: k.l,
        close: k.c,
      }))
      .sort((a, b) => (a.time as number) - (b.time as number))
      // 防御：重复 time 会让 setData 抛异常
      .filter((b, i, arr) => i === 0 || b.time !== arr[i - 1].time)
    series.setData(bars)
    chartRef.current?.timeScale().fitContent()
    // 实时合成的基准重置为新数据末根（切合约/周期后旧 bar 作废）
    const last = bars[bars.length - 1]
    currentBarRef.current = { ...last, time: last.time as number }
  }, [data])

  // WS 最新价 → 最后一根 bar 实时跳动（合并语义见 mergeTick 单测）
  useEffect(() => {
    const series = seriesRef.current
    if (!series || livePrice == null || !intervalSec) return
    const barTime = barStart(Math.floor(Date.now() / 1000), intervalSec)
    const bar = mergeTick(currentBarRef.current, livePrice, barTime)
    if (!bar) return // 迟到的旧周期 tick
    currentBarRef.current = bar
    series.update({
      time: bar.time as UTCTimestamp,
      open: bar.open,
      high: bar.high,
      low: bar.low,
      close: bar.close,
    })
  }, [livePrice, intervalSec])

  return <div ref={containerRef} className="w-full" data-testid="candle-chart" />
}
