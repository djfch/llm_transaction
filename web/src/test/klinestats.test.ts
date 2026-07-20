/**
 * K线派生统计纯函数测试：
 * changePct24h —— 真 24h 涨跌幅（基准=最后一根 t ≤ t_last−86400 的 bar；窗口不足/基准为 0 → null）；
 * liveMaValue —— 末根 MA20 实时延伸（最近 19 根历史收盘 + live close，不足 19 根 → null）。
 */
import { describe, expect, it } from 'vitest'
import type { Candle } from '../api/types'
import { changePct24h, liveMaValue } from '../utils/klineStats'

/** 构造一根 K 线（t 为 Unix 秒，c 收盘价，其余字段从简） */
function bar(t: number, c: number): Candle {
  return { t, o: c, h: c, l: c, c, v: 1 }
}

describe('changePct24h(真 24h 涨跌幅)', () => {
  it('恰好 24h 窗口：基准取 t = t_last − 86400 的那根', () => {
    const bars = [bar(86_400, 100), bar(172_800, 110)]
    expect(changePct24h(bars)).toBeCloseTo(0.1)
  })

  it('窗口内多根：基准取 ≤ t_last−86400 的最后一根（而非首根）', () => {
    // t_last=200000，基准线 113600；t=100000/110000 都满足，应取 110000（c=105）
    const bars = [bar(50_000, 999), bar(100_000, 100), bar(110_000, 105), bar(200_000, 126)]
    expect(changePct24h(bars)).toBeCloseTo(0.2)
  })

  it('窗口不足 24h（无任何 bar 早于基准线）→ null', () => {
    const bars = [bar(100_000, 100), bar(100_900, 101)]
    expect(changePct24h(bars)).toBeNull()
    expect(changePct24h([])).toBeNull()
    expect(changePct24h([bar(100_000, 100)])).toBeNull()
  })

  it('乱序/重复 time 输入：内部排序去重后结果一致', () => {
    const ordered = [bar(86_400, 100), bar(172_800, 110)]
    const messy = [bar(172_800, 110), bar(86_400, 100), bar(86_400, 100)]
    expect(changePct24h(messy)).toBeCloseTo(changePct24h(ordered) ?? 0)
  })

  it('基准收盘价为 0 → null（避免除零）', () => {
    const bars = [bar(86_400, 0), bar(172_800, 110)]
    expect(changePct24h(bars)).toBeNull()
  })

  it('下跌为负值', () => {
    const bars = [bar(86_400, 100), bar(172_800, 90)]
    expect(changePct24h(bars)).toBeCloseTo(-0.1)
  })
})

describe('liveMaValue(MA20 实时延伸)', () => {
  it('恰好 19 根历史：19 根均值与 live close 合成 20 根均线', () => {
    const history = Array.from({ length: 19 }, () => 100)
    expect(liveMaValue(history, 120)).toBeCloseTo((19 * 100 + 120) / 20)
  })

  it('超过 19 根历史：只取最近 19 根', () => {
    // 前 5 根为 1（不应计入），后 19 根为 100，live=120
    const history = [...Array.from({ length: 5 }, () => 1), ...Array.from({ length: 19 }, () => 100)]
    expect(liveMaValue(history, 120)).toBeCloseTo((19 * 100 + 120) / 20)
  })

  it('不足 19 根历史 → null（调用方跳过本根 MA 更新）', () => {
    expect(liveMaValue(Array.from({ length: 18 }, () => 100), 120)).toBeNull()
    expect(liveMaValue([], 120)).toBeNull()
  })
})
