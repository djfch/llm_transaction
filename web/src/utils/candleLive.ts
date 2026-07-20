/**
 * K 线实时跳动纯函数（与图表库解耦，供 KlinePanel 消费、单测直接覆盖）。
 * 语义与后端 CandleCache 滚动一致：同一开盘时刻的 tick 视为当前 bar 更新，
 * 更晚的开盘时刻开新 bar，早于当前 bar 的迟到 tick 忽略。
 */

/** 实时合成中的单根 K 线（lightweight-charts Bar 的最小形态） */
export interface LiveBar {
  time: number // 开盘时刻（秒级 Unix 时间）
  open: number
  high: number
  low: number
  close: number
}

/** Gate K 线周期 → 秒数（与后端 GATE_CANDLE_INTERVALS 全 15 周期对齐；未知周期 null） */
const INTERVAL_SEC: Readonly<Record<string, number>> = {
  '10s': 10,
  '1m': 60,
  '5m': 300,
  '15m': 900,
  '30m': 1800,
  '1h': 3600,
  '2h': 7200,
  '4h': 14400,
  '6h': 21600,
  '8h': 28800,
  '12h': 43200,
  '1d': 86400,
  '7d': 604800,
  '30d': 2592000,
  '1w': 604800,
}

export function intervalToSec(interval: string): number | null {
  return INTERVAL_SEC[interval] ?? null
}

/** 当前时刻所属 K 线的开盘时刻（向下取整到周期边界） */
export function barStart(nowSec: number, intervalSec: number): number {
  return Math.floor(nowSec / intervalSec) * intervalSec
}

/**
 * 把一笔最新价合并进当前 bar：
 * - 无当前 bar / 进入下一周期 → 以最新价开新 bar（高低收都重置，不沿用旧 bar）
 * - 同一周期内 → 只更新 high/low/close，open 与 time 不变
 * - 迟到的旧周期 tick → null（调用方忽略）
 */
export function mergeTick(bar: LiveBar | null, price: number, barTime: number): LiveBar | null {
  if (bar === null || barTime > bar.time) {
    return { time: barTime, open: price, high: price, low: price, close: price }
  }
  if (barTime === bar.time) {
    return { ...bar, high: Math.max(bar.high, price), low: Math.min(bar.low, price), close: price }
  }
  return null
}
