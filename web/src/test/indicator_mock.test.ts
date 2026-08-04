/**
 * 指标 mock 冒烟：mock 模式下无真后端也能出图——
 * 短名单六项齐备；序列时间与同参数 K 线对齐且含非 null 值（图能出线条）；
 * scalar（oi/atr14）随响应返回可展示值；同参数多次请求结果确定一致。
 */
import { describe, expect, it } from 'vitest'
import { mockApi } from '../api/mock'

describe('指标 mock（出图冒烟）', () => {
  it('短名单 = 默认六项，available 覆盖全部契约 key', async () => {
    const config = await mockApi.getIndicatorConfig()
    expect(config.shortlist).toEqual(['ema20', 'ema50', 'rsi14', 'macd', 'atr14', 'oi'])
    const keys = config.available.map((a) => a.key)
    for (const k of ['ema9', 'ema20', 'ema50', 'boll', 'macd', 'rsi7', 'rsi14', 'kdj', 'roc10', 'obv', 'atr14', 'vol_ratio', 'oi']) {
      expect(keys).toContain(k)
    }
  })

  it('严格按请求 keys 返回（不额外补齐）：scalar 请求才有；同参数结果确定一致', async () => {
    const candles = await mockApi.getCandles('BTC_USDT', '1h', 200)
    const keys = ['ema20', 'ema50', 'rsi14', 'macd']
    const resp = await mockApi.getIndicatorSeries('BTC_USDT', '1h', keys, 200)

    // overlay/pane 序列时间与 K 线一致（可直接上图）
    const times = new Set(candles.map((c) => c.t))
    for (const key of keys) {
      const entry = resp.series[key]
      expect(entry).toBeDefined()
      for (const points of Object.values(entry.fields)) {
        expect(points.every((p) => times.has(p.time))).toBe(true)
        expect(points.some((p) => p.value !== null)).toBe(true) // 有值才能出线条
      }
    }
    // macd 三字段齐备
    expect(Object.keys(resp.series.macd.fields)).toEqual(['dif', 'dea', 'hist'])
    // 未请求的 scalar 不出现（与真实后端契约一致：测试桩不得返回未请求的数据）
    expect(resp.series.oi).toBeUndefined()
    expect(resp.series.atr14).toBeUndefined()

    // 显式请求完整短名单：scalar 有值（oi 有 current、atr14 有序列）
    const full = await mockApi.getIndicatorSeries('BTC_USDT', '1h', [...keys, 'atr14', 'oi'], 200)
    expect(full.series.oi.kind).toBe('scalar')
    expect(full.series.oi.current).toBeGreaterThan(0)
    const atr = full.series.atr14.fields.atr14
    expect(atr.filter((p) => p.value !== null).length).toBeGreaterThan(0)

    const again = await mockApi.getIndicatorSeries('BTC_USDT', '1h', keys, 200)
    expect(again.series.ema20.fields.ema20).toEqual(resp.series.ema20.fields.ema20)
  })
})
