/**
 * K 线图：lightweight-charts v5 CandlestickSeries 封装（深色主题与 EquityChart 一致，自适应宽度）。
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

export default function CandleChart({ data }: { data: Candle[] }) {
  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const seriesRef = useRef<ISeriesApi<'Candlestick'> | null>(null)

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
  }, [data])

  return <div ref={containerRef} className="w-full" data-testid="candle-chart" />
}
