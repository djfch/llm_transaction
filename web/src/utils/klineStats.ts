/**
 * K 线派生统计纯函数（与图表库解耦，供 KlinePanel 消费、单测直接覆盖）：
 * - sortedUnique / toCandlePoint / toVolumePoint / ma20Points：序列装配（自 KlinePanel 迁入，集中可测）
 * - changePct24h：真 24h 涨跌幅（基准=最后一根 t ≤ t_last−86400 的 bar）
 * - liveMaValue：末根 MA20 实时延伸（最近 19 根历史收盘 + live close）
 */
import type { UTCTimestamp } from 'lightweight-charts'
import type { Candle } from '../api/types'

/** 24 小时秒数（changePct24h 的窗口宽度） */
const DAY_SEC = 86_400

/** bars → 时间升序且去重（重复 time 会让 setData 抛异常） */
export function sortedUnique(bars: Candle[]): Candle[] {
  return [...bars].sort((a, b) => a.t - b.t).filter((b, i, arr) => i === 0 || b.t !== arr[i - 1].t)
}

/** K 线 → 蜡烛序列点 */
export function toCandlePoint(k: Candle) {
  return { time: k.t as UTCTimestamp, open: k.o, high: k.h, low: k.l, close: k.c }
}

/** K 线 → 成交量点（涨绿跌红半透明） */
export function toVolumePoint(k: Candle) {
  const color = k.c >= k.o ? 'rgba(52, 211, 153, 0.45)' : 'rgba(251, 113, 133, 0.45)'
  return { time: k.t as UTCTimestamp, value: k.v, color }
}

/** 客户端计算 MA20：前 19 根无值（从第 20 根起输出，缺口自然不画） */
export function ma20Points(bars: Candle[]): Array<{ time: UTCTimestamp; value: number }> {
  const out: Array<{ time: UTCTimestamp; value: number }> = []
  let sum = 0
  for (let i = 0; i < bars.length; i += 1) {
    sum += bars[i].c
    if (i >= 20) sum -= bars[i - 20].c
    if (i >= 19) out.push({ time: bars[i].t as UTCTimestamp, value: sum / 20 })
  }
  return out
}

/**
 * 真 24h 涨跌幅（0-1 比例）：以末根收盘对比 24h 前基准。
 * 基准 = 最后一根 t ≤ t_last−86400 的 bar（输入任意顺序/可含重复 time，内部排序去重）；
 * 窗口不足 24h（历史太短）或基准收盘价为 0 → null（表头不渲染该指标）。
 */
export function changePct24h(bars: Candle[]): number | null {
  const data = sortedUnique(bars)
  const last = data[data.length - 1]
  if (!last) return null
  const baseline = last.t - DAY_SEC
  let base: Candle | undefined
  for (const b of data) {
    if (b.t > baseline) break
    base = b
  }
  if (!base || base.c === 0) return null
  return (last.c - base.c) / base.c
}

/**
 * 末根 MA20 实时延伸：最近 19 根历史收盘 + live close 的 20 点均值。
 * historyCloses 为「live bar 之前」已完成 bar 的收盘价（升序）；不足 19 根 → null（跳过本根更新）。
 */
export function liveMaValue(historyCloses: number[], liveClose: number): number | null {
  if (historyCloses.length < 19) return null
  const tail = historyCloses.slice(-19)
  return (tail.reduce((sum, c) => sum + c, 0) + liveClose) / 20
}
