import type { EquityPoint } from '../../api/types'

export function equityChangePct(currentEquity: number, initialEquity: number): number | undefined {
  if (initialEquity === 0) return undefined
  return ((currentEquity - initialEquity) / initialEquity) * 100
}

export function withCurrentEquity(
  history: EquityPoint[],
  asOf: string,
  currentEquity: number,
): EquityPoint[] {
  const asOfTime = new Date(asOf).getTime()
  const current = { time: asOf, equity: currentEquity }
  const earlier = history
    .filter((point) => {
      const pointTime = new Date(point.time).getTime()
      return !Number.isNaN(pointTime) && pointTime < asOfTime
    })
    .sort((left, right) => new Date(left.time).getTime() - new Date(right.time).getTime())
  return [...earlier, current]
}
