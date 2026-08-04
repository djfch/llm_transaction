/**
 * 指标序列装配纯函数（与图表库解耦，供 useIndicatorSeries / useIndicatorChart 消费、单测直接覆盖）：
 * - displayName：后端 label（'EMA20（指数均线）'）→ 徽标展示名（EMA20；oi → 持仓量）
 * - buildOverlaySeries：overlay 指标 → 主图 LineSeries 数据（按 K 线 time 对齐，null 跳过）
 * - buildPaneSeries：pane 指标 → 副图配置描述（hist 用 Histogram 并按符号着色，其余 Line）
 * - scalarBadges：scalar 指标 → 徽标文本（oi 取 current，其余取序列末值；无值 → 无数据）
 */
import type { UTCTimestamp } from 'lightweight-charts'
import type { Candle, IndicatorConfig, IndicatorSeriesEntry, IndicatorSeriesResponse } from '../api/types'

/** overlay 线色板：按短名单顺序循环取色；与既有 MA20 青色（#22d3ee）错开避免混淆 */
export const OVERLAY_PALETTE = ['#fbbf24', '#f472b6', '#a78bfa', '#e879f9', '#94a3b8', '#64748b']

/** pane 线色板：dif/dea、k/d/j 等多字段按序取色 */
export const PANE_PALETTE = ['#22d3ee', '#f59e0b', '#a78bfa', '#34d399', '#f472b6', '#94a3b8']

/** MACD hist 柱色：随符号涨绿跌红（与成交量 histogram 同族半透明） */
export const HIST_UP_COLOR = 'rgba(52, 211, 153, 0.6)'
export const HIST_DOWN_COLOR = 'rgba(251, 113, 133, 0.6)'

/** 指标线宽：细线不抢蜡烛主体（与 MA20 一致） */
export const INDICATOR_LINE_WIDTH = 1 as const

/** 折线数据点（time 为 Unix 秒，与 K 线一致） */
export interface LinePoint {
  time: UTCTimestamp
  value: number
}

/** overlay 一条线的装配结果：id 唯一标识 `${key}.${field}`（系列同步/移除用） */
export interface OverlayLine {
  id: string
  key: string
  field: string
  color: string
  data: LinePoint[]
}

/** pane 内一条系列：histogram=true 时按符号着色（data 逐点带 color，忽略 color 字段） */
export interface PaneLine {
  field: string
  color: string
  histogram: boolean
  data: Array<LinePoint & { color?: string }>
}

/** pane 指标装配结果：key + 若干系列（挂到同一副图） */
export interface PaneSpec {
  key: string
  lines: PaneLine[]
}

/** scalar 徽标：label 展示名 + text 已格式化文本（无数据时为占位文案） */
export interface ScalarBadge {
  key: string
  label: string
  text: string
}

/**
 * 展示名：oi 按 AGENTS §7「英文键+括号中文释义只留中文」显示持仓量；
 * 其余取 label 的标识段（'EMA20(指数均线)' → 'EMA20'，独立英文技术标识保持原样），label 缺失回退 key 大写。
 * 括号同时兼容半角（生产后端）与全角（历史 mock）。
 */
export function displayName(key: string, label: string): string {
  if (key === 'oi') return '持仓量'
  const head = label.split(/[（(]/)[0]?.trim() ?? ''
  return head || key.toUpperCase()
}

/** 指标数值格式化：千分位、至多两位小数（123456 → "123,456"；892.5377 → "892.54"） */
function fmtMetric(n: number): string {
  return n.toLocaleString('zh-CN', { maximumFractionDigits: 2 })
}

/** 序列点 → 折线点：按 K 线 time 集合对齐（只保留有对应 K 线的点），null 跳过，时间升序 */
function alignPoints(points: IndicatorSeriesEntry['fields'][string], candleTimes: Set<number>): LinePoint[] {
  return (points ?? [])
    .filter((p) => p.value !== null && candleTimes.has(p.time))
    .sort((a, b) => a.time - b.time)
    .map((p) => ({ time: p.time as UTCTimestamp, value: p.value as number }))
}

/**
 * overlay 指标 → 主图线数组：items 按短名单顺序（决定调色板取色），
 * 非 overlay 条目防御性跳过；每个字段一条线（boll 出 upper/mid/lower 三条）。
 */
export function buildOverlaySeries(
  items: Array<{ key: string; entry: IndicatorSeriesEntry }>,
  candles: Candle[],
): OverlayLine[] {
  const candleTimes = new Set(candles.map((c) => c.t))
  const out: OverlayLine[] = []
  let colorIdx = 0
  for (const { key, entry } of items) {
    if (entry.kind !== 'overlay') continue
    for (const [field, points] of Object.entries(entry.fields)) {
      out.push({
        id: `${key}.${field}`,
        key,
        field,
        color: OVERLAY_PALETTE[colorIdx % OVERLAY_PALETTE.length],
        data: alignPoints(points, candleTimes),
      })
      colorIdx += 1
    }
  }
  return out
}

/**
 * pane 指标 → 副图配置：字段顺序即后端 fields 插入序（dif/dea/hist、k/d/j）；
 * 字段名 hist 用 Histogram（逐点按符号涨绿跌红），其余用 Line；null 跳过、时间升序。
 */
export function buildPaneSeries(key: string, entry: IndicatorSeriesEntry): PaneSpec {
  const lines: PaneLine[] = Object.entries(entry.fields).map(([field, points], i) => {
    const histogram = field === 'hist'
    const sorted = (points ?? [])
      .filter((p) => p.value !== null)
      .sort((a, b) => a.time - b.time)
    return {
      field,
      color: PANE_PALETTE[i % PANE_PALETTE.length],
      histogram,
      data: sorted.map((p) => ({
        time: p.time as UTCTimestamp,
        value: p.value as number,
        ...(histogram ? { color: (p.value as number) >= 0 ? HIST_UP_COLOR : HIST_DOWN_COLOR } : {}),
      })),
    }
  })
  return { key, lines }
}

/**
 * scalar 指标 → 徽标数组：按短名单顺序；oi 取 current，其余取首字段序列的最后一个非 null 值
 * （current 缺失时同样回落序列末值）；值缺失 → 「无数据」。seriesResp 为 null（加载中/失败）时仍出占位徽标。
 */
export function scalarBadges(config: IndicatorConfig, seriesResp: IndicatorSeriesResponse | null): ScalarBadge[] {
  const out: ScalarBadge[] = []
  for (const key of config.shortlist) {
    const meta = config.available.find((a) => a.key === key)
    const entry = seriesResp?.series[key]
    const kind = meta?.kind ?? entry?.kind
    if (kind !== 'scalar') continue
    const label = displayName(key, meta?.label ?? entry?.label ?? '')
    const fieldValues = entry ? Object.values(entry.fields)[0] : undefined
    const last = fieldValues?.filter((p) => p.value !== null).at(-1)
    const value = entry?.current ?? last?.value ?? null
    out.push({ key, label, text: value === null ? '无数据' : fmtMetric(value) })
  }
  return out
}
