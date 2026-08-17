/** K 线 UTC+8 展示格式测试：原始 Unix 时间戳不改，只转换用户可见标签。 */
import { describe, expect, it } from 'vitest'
import { formatUtc8Crosshair, formatUtc8Tick } from '../utils/klineTime'
import { ma20Points, toCandlePoint, toVolumePoint } from '../utils/klineStats'

describe('K 线 UTC+8 时间展示', () => {
  it('Unix 00:00 UTC 在十字光标显示为同日 08:00', () => {
    expect(formatUtc8Crosshair(1_788_134_400)).toBe('2026-08-31 08:00')
  })

  it('横轴按刻度粒度输出短标签', () => {
    const ts = 1_788_134_400
    expect(formatUtc8Tick(ts, 0)).toBe('2026')
    expect(formatUtc8Tick(ts, 1)).toBe('08月')
    expect(formatUtc8Tick(ts, 2)).toBe('08-31')
    expect(formatUtc8Tick(ts, 3)).toBe('08:00')
    expect(formatUtc8Tick(ts, 4)).toBe('08:00:00')
  })

  it('K线、成交量和 MA 继续使用原始 Unix 秒，不整体加八小时', () => {
    const start = 1_788_134_400
    const bars = Array.from({ length: 20 }, (_, index) => ({
      t: start + index * 3600,
      o: 100 + index,
      h: 101 + index,
      l: 99 + index,
      c: 100 + index,
      v: 10 + index,
    }))
    expect(toCandlePoint(bars[0]).time).toBe(start)
    expect(toVolumePoint(bars[0]).time).toBe(start)
    expect(ma20Points(bars)[0].time).toBe(start + 19 * 3600)
  })
})
