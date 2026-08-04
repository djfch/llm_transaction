/**
 * 指标序列装配纯函数测试：
 * displayName —— oi→持仓量，其余取 label 英文标识段；
 * buildOverlaySeries —— 按 K 线 time 对齐、null 跳过、多字段多线、调色板按序取色；
 * buildPaneSeries —— hist 用 Histogram 并按符号着色，其余 Line；
 * scalarBadges —— oi 取 current、其余取序列末值、无值 → 无数据。
 */
import { describe, expect, it } from 'vitest'
import type { Candle, IndicatorConfig, IndicatorSeriesEntry, IndicatorSeriesResponse } from '../api/types'
import {
  buildOverlaySeries,
  buildPaneSeries,
  displayName,
  HIST_DOWN_COLOR,
  HIST_UP_COLOR,
  OVERLAY_PALETTE,
  PANE_PALETTE,
  scalarBadges,
} from '../utils/indicatorSeries'

/** 构造一根 K 线（t 为 Unix 秒，其余字段从简） */
function bar(t: number): Candle {
  return { t, o: 1, h: 1, l: 1, c: 1, v: 1 }
}

/** 构造指标序列点（time 为 Unix 秒） */
function pt(time: number, value: number | null) {
  return { time, value }
}

/** 构造序列条目（按 partial 覆盖） */
function entry(partial: Partial<IndicatorSeriesEntry>): IndicatorSeriesEntry {
  return { label: '', kind: 'overlay', fields: {}, current: null, ...partial }
}

describe('displayName(展示名)', () => {
  it("oi 显示「持仓量」（AGENTS §7：英文键+括号中文释义只留中文）", () => {
    expect(displayName('oi', '持仓量')).toBe('持仓量')
  })

  it("其余取 label 的英文标识段：生产半角 'EMA20(指数均线)' → 'EMA20'", () => {
    expect(displayName('ema20', 'EMA20(指数均线)')).toBe('EMA20')
    expect(displayName('macd', 'MACD(异同均线)')).toBe('MACD')
  })

  it("兼容历史全角括号：'EMA20（指数均线）' → 'EMA20'", () => {
    expect(displayName('ema20', 'EMA20（指数均线）')).toBe('EMA20')
  })

  it('label 无括号原样保留；label 缺失回退 key 大写', () => {
    expect(displayName('rsi14', 'RSI14')).toBe('RSI14')
    expect(displayName('vol_ratio', '')).toBe('VOL_RATIO')
  })
})

describe('buildOverlaySeries(主图叠加线)', () => {
  it('按 K 线 time 对齐：只保留有对应 K 线的点，null 跳过，时间升序', () => {
    const items = [
      {
        key: 'ema20',
        entry: entry({
          kind: 'overlay',
          fields: { ema20: [pt(300, 3), pt(100, 1), pt(200, null), pt(999, 9)] },
        }),
      },
    ]
    const [line] = buildOverlaySeries(items, [bar(100), bar(200), bar(300)])
    expect(line.id).toBe('ema20.ema20')
    expect(line.color).toBe(OVERLAY_PALETTE[0])
    expect(line.data).toEqual([
      { time: 100, value: 1 },
      { time: 300, value: 3 },
    ])
  })

  it('boll 多字段出三条线，颜色按序取色；非 overlay 条目跳过', () => {
    const items = [
      { key: 'ema50', entry: entry({ kind: 'overlay', fields: { ema50: [pt(100, 1)] } }) },
      {
        key: 'boll',
        entry: entry({
          kind: 'overlay',
          fields: { upper: [pt(100, 2)], mid: [pt(100, 1)], lower: [pt(100, 0)] },
        }),
      },
      { key: 'rsi14', entry: entry({ kind: 'pane', fields: { rsi14: [pt(100, 55)] } }) },
    ]
    const lines = buildOverlaySeries(items, [bar(100)])
    expect(lines.map((l) => l.id)).toEqual(['ema50.ema50', 'boll.upper', 'boll.mid', 'boll.lower'])
    expect(lines.map((l) => l.color)).toEqual([
      OVERLAY_PALETTE[0],
      OVERLAY_PALETTE[1],
      OVERLAY_PALETTE[2],
      OVERLAY_PALETTE[3],
    ])
  })

  it('空输入 → 空数组', () => {
    expect(buildOverlaySeries([], [bar(100)])).toEqual([])
  })
})

describe('buildPaneSeries(副图配置)', () => {
  it('macd：dif/dea 用 Line，hist 用 Histogram 并按符号涨绿跌红', () => {
    const spec = buildPaneSeries(
      'macd',
      entry({
        kind: 'pane',
        fields: {
          dif: [pt(100, 1.5)],
          dea: [pt(100, 1.2)],
          hist: [pt(100, 0.3), pt(200, -0.2), pt(300, null)],
        },
      }),
    )
    expect(spec.key).toBe('macd')
    expect(spec.lines.map((l) => l.field)).toEqual(['dif', 'dea', 'hist'])
    expect(spec.lines[0].histogram).toBe(false)
    expect(spec.lines[0].color).toBe(PANE_PALETTE[0])
    expect(spec.lines[1].color).toBe(PANE_PALETTE[1])
    expect(spec.lines[2].histogram).toBe(true)
    expect(spec.lines[2].data).toEqual([
      { time: 100, value: 0.3, color: HIST_UP_COLOR },
      { time: 200, value: -0.2, color: HIST_DOWN_COLOR },
    ])
  })

  it('rsi14 单线 Line；乱序输入按时间升序输出', () => {
    const spec = buildPaneSeries(
      'rsi14',
      entry({ kind: 'pane', fields: { rsi14: [pt(200, 60), pt(100, 55)] } }),
    )
    expect(spec.lines).toHaveLength(1)
    expect(spec.lines[0].histogram).toBe(false)
    expect(spec.lines[0].data.map((p) => p.time)).toEqual([100, 200])
  })
})

describe('scalarBadges(scalar 徽标)', () => {
  const config: IndicatorConfig = {
    shortlist: ['ema20', 'atr14', 'vol_ratio', 'oi'],
    available: [
      { key: 'ema20', label: 'EMA20(指数均线)', kind: 'overlay', fields: ['ema20'] },
      { key: 'atr14', label: 'ATR14(平均真实波幅)', kind: 'scalar', fields: ['atr14'] },
      { key: 'vol_ratio', label: '量比(相对20根均量)', kind: 'scalar', fields: ['vol_ratio'] },
      { key: 'oi', label: '持仓量', kind: 'scalar', fields: [] },
    ],
  }

  function resp(series: IndicatorSeriesResponse['series']): IndicatorSeriesResponse {
    return { contract: 'BTC_USDT', interval: '1h', series }
  }

  it('oi 取 current（千分位）；atr14 取序列最后一个非 null 值；overlay 不进徽标', () => {
    const badges = scalarBadges(
      config,
      resp({
        atr14: entry({
          kind: 'scalar',
          label: 'ATR14(平均真实波幅)',
          fields: { atr14: [pt(100, 800), pt(200, 892.5377), pt(300, null)] },
        }),
        vol_ratio: entry({ kind: 'scalar', fields: { vol_ratio: [pt(100, null)] } }),
        oi: entry({ kind: 'scalar', label: '持仓量', fields: {}, current: 123456 }),
      }),
    )
    expect(badges).toEqual([
      { key: 'atr14', label: 'ATR14', text: '892.54' },
      { key: 'vol_ratio', label: '量比', text: '无数据' },
      { key: 'oi', label: '持仓量', text: '123,456' },
    ])
  })

  it('current 缺失时回落序列末值；序列响应为 null 时全部「无数据」', () => {
    const withCurrent = scalarBadges(
      config,
      resp({ oi: entry({ kind: 'scalar', fields: { oi: [pt(100, 42)] } }) }),
    )
    expect(withCurrent.at(-1)?.text).toBe('42')
    const empty = scalarBadges(config, null)
    expect(empty.every((b) => b.text === '无数据')).toBe(true)
    expect(empty.map((b) => b.key)).toEqual(['atr14', 'vol_ratio', 'oi'])
  })
})
