/**
 * 指标 mock：短名单配置 + 由 mock K 线确定性计算的指标序列（与 mockReview 同工厂模式），
 * 由 mock.ts 经 createIndicatorMock 装配进 mockApi；后端未就绪时图上能看到真实形态的线条。
 * 计算口径从简（简单均值 RSI/KDJ 等），仅用于演示展示链路，不与后端指标服务对数。
 */
import type {
  ApiClient,
  Candle,
  IndicatorSeriesPoint,
  IndicatorSeriesResponse,
} from './types'

/** mockApi 中指标相关的方法子集 */
type IndicatorMockHandlers = Pick<ApiClient, 'getIndicatorConfig' | 'getIndicatorSeries'>

type Reply = <T>(value: T) => Promise<T>
type BuildCandles = (contract: string, interval: string, limit: number) => Candle[]
type Values = Array<number | null>

/** 指标元数据：label/kind/fields 与后端 REGISTRY 逐字对齐（生产标签为半角括号「英文标识(中文释义)」） */
const META: Record<string, { label: string; kind: 'overlay' | 'pane' | 'scalar'; fields: string[] }> = {
  ema9: { label: 'EMA9(指数均线)', kind: 'overlay', fields: ['ema9'] },
  ema20: { label: 'EMA20(指数均线)', kind: 'overlay', fields: ['ema20'] },
  ema50: { label: 'EMA50(指数均线)', kind: 'overlay', fields: ['ema50'] },
  boll: { label: 'BOLL(布林带)', kind: 'overlay', fields: ['upper', 'mid', 'lower'] },
  macd: { label: 'MACD(异同均线)', kind: 'pane', fields: ['dif', 'dea', 'hist'] },
  rsi7: { label: 'RSI7(相对强弱)', kind: 'pane', fields: ['rsi7'] },
  rsi14: { label: 'RSI14(相对强弱)', kind: 'pane', fields: ['rsi14'] },
  kdj: { label: 'KDJ(随机指标)', kind: 'pane', fields: ['k', 'd', 'j'] },
  roc10: { label: 'ROC10(变动率)', kind: 'pane', fields: ['roc10'] },
  obv: { label: 'OBV(能量潮)', kind: 'pane', fields: ['obv'] },
  atr14: { label: 'ATR14(平均真实波幅)', kind: 'scalar', fields: ['atr14'] },
  vol_ratio: { label: '量比(相对20根均量)', kind: 'scalar', fields: ['vol_ratio'] },
  oi: { label: '持仓量', kind: 'scalar', fields: [] },
}

/** mock 短名单 = 后端默认六项（ema20/ema50 主图、rsi14/macd 副图、atr14/oi 徽标） */
const SHORTLIST = ['ema20', 'ema50', 'rsi14', 'macd', 'atr14', 'oi']

/** EMA 序列（前 n-1 根为 null；初值取前 n 根简单均值） */
function emaValues(closes: number[], n: number): Values {
  const out: Values = new Array<number | null>(closes.length).fill(null)
  if (closes.length < n) return out
  let prev = closes.slice(0, n).reduce((s, c) => s + c, 0) / n
  out[n - 1] = prev
  const k = 2 / (n + 1)
  for (let i = n; i < closes.length; i += 1) {
    prev = closes[i] * k + prev * (1 - k)
    out[i] = prev
  }
  return out
}

/** SMA 序列（前 n-1 根为 null） */
function smaValues(values: number[], n: number): Values {
  const out: Values = new Array<number | null>(values.length).fill(null)
  let sum = 0
  for (let i = 0; i < values.length; i += 1) {
    sum += values[i]
    if (i >= n) sum -= values[i - n]
    if (i >= n - 1) out[i] = sum / n
  }
  return out
}

/** 稀疏 SMA：输入可含 null（暖机段），对连续非 null 段做窗口均值 */
function smaSparse(values: Values, n: number): Values {
  const out: Values = new Array<number | null>(values.length).fill(null)
  const window: number[] = []
  for (let i = 0; i < values.length; i += 1) {
    const v = values[i]
    if (v === null) {
      window.length = 0
      continue
    }
    window.push(v)
    if (window.length > n) window.shift()
    if (window.length === n) out[i] = window.reduce((s, x) => s + x, 0) / n
  }
  return out
}

/** RSI（简单滑动窗口口径，前 n 根为 null） */
function rsiValues(closes: number[], n: number): Values {
  const out: Values = new Array<number | null>(closes.length).fill(null)
  let gain = 0
  let loss = 0
  for (let i = 1; i < closes.length; i += 1) {
    const diff = closes[i] - closes[i - 1]
    gain += Math.max(diff, 0)
    loss += Math.max(-diff, 0)
    if (i > n) {
      const old = closes[i - n] - closes[i - n - 1]
      gain -= Math.max(old, 0)
      loss -= Math.max(-old, 0)
    }
    if (i >= n) out[i] = loss === 0 ? 100 : 100 - 100 / (1 + gain / loss)
  }
  return out
}

/** BOLL(20, 2)：mid=SMA20，upper/lower=mid±2σ */
function bollValues(closes: number[]): { upper: Values; mid: Values; lower: Values } {
  const mid = smaValues(closes, 20)
  const upper: Values = new Array<number | null>(closes.length).fill(null)
  const lower: Values = new Array<number | null>(closes.length).fill(null)
  for (let i = 19; i < closes.length; i += 1) {
    const m = mid[i] as number
    const variance = closes.slice(i - 19, i + 1).reduce((s, c) => s + (c - m) ** 2, 0) / 20
    const sigma = Math.sqrt(variance)
    upper[i] = m + 2 * sigma
    lower[i] = m - 2 * sigma
  }
  return { upper, mid, lower }
}

/** MACD(12,26,9)：dif=EMA12−EMA26，dea=dif 的 EMA9（稀疏口径），hist=dif−dea */
function macdValues(closes: number[]): { dif: Values; dea: Values; hist: Values } {
  const fast = emaValues(closes, 12)
  const slow = emaValues(closes, 26)
  const dif: Values = closes.map((_, i) => (fast[i] === null || slow[i] === null ? null : (fast[i] as number) - (slow[i] as number)))
  const firstDif = dif.findIndex((v) => v !== null)
  const tail = firstDif < 0 ? [] : (dif.slice(firstDif) as number[])
  const deaTail = emaValues(tail, 9)
  const dea: Values = [...new Array<number | null>(Math.max(firstDif, 0)).fill(null), ...deaTail]
  const hist: Values = closes.map((_, i) => (dif[i] === null || dea[i] === null ? null : (dif[i] as number) - (dea[i] as number)))
  return { dif, dea, hist }
}

/** KDJ(9,3,3)：rsv=(c−llv)/(hhv−llv)×100（hhv=llv 时取 50），k/d 为稀疏 SMA3，j=3k−2d */
function kdjValues(candles: Candle[]): { k: Values; d: Values; j: Values } {
  const rsv: Values = candles.map((c, i) => {
    if (i < 8) return null
    const slice = candles.slice(i - 8, i + 1)
    const hhv = Math.max(...slice.map((b) => b.h))
    const llv = Math.min(...slice.map((b) => b.l))
    return hhv === llv ? 50 : ((c.c - llv) / (hhv - llv)) * 100
  })
  const k = smaSparse(rsv, 3)
  const d = smaSparse(k, 3)
  const j: Values = candles.map((_, i) => (k[i] === null || d[i] === null ? null : 3 * (k[i] as number) - 2 * (d[i] as number)))
  return { k, d, j }
}

/** ATR14：tr=max(h−l, |h−c'|, |l−c'|)，取 SMA14 */
function atrValues(candles: Candle[], n: number): Values {
  const tr = candles.map((c, i) =>
    i === 0 ? c.h - c.l : Math.max(c.h - c.l, Math.abs(c.h - candles[i - 1].c), Math.abs(c.l - candles[i - 1].c)),
  )
  return smaValues(tr, n)
}

/** 序列值 → 图表点（null 保留；数值保留 4 位小数以控 JSON 体积） */
function toPoints(candles: Candle[], values: Values): IndicatorSeriesPoint[] {
  return candles.map((c, i) => ({
    time: c.t,
    value: values[i] === null ? null : Math.round((values[i] as number) * 10_000) / 10_000,
  }))
}

/** 按 key 计算字段序列（未知 key → 空对象，由调用方过滤） */
function buildFields(key: string, candles: Candle[]): Record<string, IndicatorSeriesPoint[]> {
  const closes = candles.map((c) => c.c)
  switch (key) {
    case 'ema9':
      return { ema9: toPoints(candles, emaValues(closes, 9)) }
    case 'ema20':
      return { ema20: toPoints(candles, emaValues(closes, 20)) }
    case 'ema50':
      return { ema50: toPoints(candles, emaValues(closes, 50)) }
    case 'boll': {
      const { upper, mid, lower } = bollValues(closes)
      return { upper: toPoints(candles, upper), mid: toPoints(candles, mid), lower: toPoints(candles, lower) }
    }
    case 'macd': {
      const { dif, dea, hist } = macdValues(closes)
      return { dif: toPoints(candles, dif), dea: toPoints(candles, dea), hist: toPoints(candles, hist) }
    }
    case 'rsi7':
      return { rsi7: toPoints(candles, rsiValues(closes, 7)) }
    case 'rsi14':
      return { rsi14: toPoints(candles, rsiValues(closes, 14)) }
    case 'kdj': {
      const { k, d, j } = kdjValues(candles)
      return { k: toPoints(candles, k), d: toPoints(candles, d), j: toPoints(candles, j) }
    }
    case 'roc10':
      return { roc10: toPoints(candles, closes.map((c, i) => (i < 10 ? null : (c / closes[i - 10] - 1) * 100))) }
    case 'obv': {
      const obv: Values = new Array<number | null>(candles.length).fill(null)
      let acc = candles[0]?.v ?? 0
      obv[0] = candles.length > 0 ? acc : null
      for (let i = 1; i < candles.length; i += 1) {
        acc += Math.sign(candles[i].c - candles[i - 1].c) * candles[i].v
        obv[i] = acc
      }
      return { obv: toPoints(candles, obv) }
    }
    case 'atr14':
      return { atr14: toPoints(candles, atrValues(candles, 14)) }
    case 'vol_ratio': {
      const ma5 = smaValues(candles.map((c) => c.v), 5)
      return { vol_ratio: toPoints(candles, candles.map((c, i) => (ma5[i] ? c.v / (ma5[i] as number) : null))) }
    }
    default:
      return {}
  }
}

/** oi 当前值：按合约名散列的确定性假持仓量（同一合约多次请求一致） */
function oiCurrent(contract: string): number {
  let hash = 0
  for (const ch of contract) hash = (hash * 31 + ch.charCodeAt(0)) % 1_000_003
  return 100_000 + (hash % 50_000)
}

/** 单个指标的序列条目（label/kind 取自 META；oi 无序列，current 按合约散列） */
function buildEntry(
  key: string,
  contract: string,
  candles: Candle[],
): IndicatorSeriesResponse['series'][string] | null {
  const meta = META[key]
  if (!meta) return null
  if (key === 'oi') return { label: meta.label, kind: meta.kind, fields: {}, current: oiCurrent(contract) }
  return { label: meta.label, kind: meta.kind, fields: buildFields(key, candles), current: null }
}

/** 装配指标 mock 方法：注入 reply 与 mock.ts 的 buildCandles（与 createReviewMock 同形态） */
export function createIndicatorMock(reply: Reply, buildCandles: BuildCandles): IndicatorMockHandlers {
  return {
    getIndicatorConfig: () =>
      reply({
        shortlist: [...SHORTLIST],
        available: Object.entries(META).map(([key, meta]) => ({ key, ...meta })),
      }),
    getIndicatorSeries: (contract, interval, keys, limit = 200) => {
      const candles = buildCandles(contract, interval, limit)
      const series: IndicatorSeriesResponse['series'] = {}
      // 严格按请求的 keys 返回（与真实后端一致：不额外补齐，测试桩不得返回未请求的数据）
      for (const key of keys) {
        const entry = buildEntry(key, contract, candles)
        if (entry) series[key] = entry
      }
      return reply({ contract, interval, series })
    },
  }
}
