import { describe, expect, it } from 'vitest'
import { equityChangePct, withCurrentEquity } from '../components/console/equityPresentation'

describe('权益实时展示口径', () => {
  it('累计权益收益使用 initial_equity，而不是历史曲线首点', () => {
    expect(equityChangePct(9900, 9000)).toBeCloseTo(10)
    expect(equityChangePct(9900, 0)).toBeUndefined()
  })

  it('把当前组合权益追加到历史曲线末端', () => {
    const points = [
      { time: '2026-07-23T00:00:00.000Z', equity: 9000 },
      { time: '2026-07-23T01:00:00.000Z', equity: 9500 },
    ]

    const live = withCurrentEquity(points, '2026-07-24T00:00:00.000Z', 9900)
    expect(live).toHaveLength(3)
    expect(live.at(-1)).toEqual({ time: '2026-07-24T00:00:00.000Z', equity: 9900 })
    expect(points).toHaveLength(2)
  })

  it('丢弃快照时点之后的占位点，保证权威快照位于曲线末端', () => {
    const live = withCurrentEquity(
      [
        { time: '2026-07-23T00:00:00.000Z', equity: 9000 },
        { time: '2026-07-25T00:00:00.000Z', equity: 12000 },
      ],
      '2026-07-24T00:00:00.000Z',
      9900,
    )

    expect(live).toEqual([
      { time: '2026-07-23T00:00:00.000Z', equity: 9000 },
      { time: '2026-07-24T00:00:00.000Z', equity: 9900 },
    ])
  })
})
