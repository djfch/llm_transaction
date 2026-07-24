import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import EquityMiniChart from '../components/console/EquityMiniChart'

describe('EquityMiniChart', () => {
  it('使用统一的初始权益口径展示累计收益率', () => {
    render(
      <EquityMiniChart
        points={[
          { time: '2026-07-23T00:00:00.000Z', equity: 9000 },
          { time: '2026-07-24T00:00:00.000Z', equity: 11000 },
        ]}
        equityChangePct={10}
      />,
    )

    expect(screen.getByText('+10.00%')).toBeInTheDocument()
    expect(screen.queryByText('+22.22%')).not.toBeInTheDocument()
  })
})
