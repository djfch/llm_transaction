import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { PriceAlert } from '../api/types'
import PriceAlertsPanel from '../components/console/PriceAlertsPanel'

const aboveAlert: PriceAlert = {
  id: 1,
  contract: 'BTC_USDT',
  direction: 'above',
  price: 122_000,
  time: '2026-07-27T01:00:00.000Z',
}

const belowAlert: PriceAlert = {
  id: 2,
  contract: 'ETH_USDT',
  direction: 'below',
  price: 3_300,
  time: '2026-07-27T02:00:00.000Z',
}

describe('PriceAlertsPanel(价格唤醒)', () => {
  it('空列表时显示标题与空态', () => {
    render(<PriceAlertsPanel alerts={[]} />)
    expect(screen.getByText('价格唤醒')).toBeInTheDocument()
    expect(screen.getByText('当前无价格唤醒')).toBeInTheDocument()
  })

  it('渲染合约、方向徽标与触发价，不显示英文枚举', () => {
    render(<PriceAlertsPanel alerts={[aboveAlert, belowAlert]} />)
    expect(screen.getByText('BTC_USDT')).toBeInTheDocument()
    expect(screen.getByText('ETH_USDT')).toBeInTheDocument()
    expect(screen.getByText('上破')).toBeInTheDocument()
    expect(screen.getByText('下破')).toBeInTheDocument()
    expect(screen.getAllByText('待触发')).toHaveLength(2)
    expect(screen.getAllByText('触发价')).toHaveLength(2)
    expect(screen.getAllByText('设置时间')).toHaveLength(2)
    expect(screen.getByText('122,000.00')).toBeInTheDocument()
    expect(screen.getByText('3,300.00')).toBeInTheDocument()
    // 数量徽标
    expect(screen.getByText('2')).toBeInTheDocument()
    // 英文枚举值不得出现在用户可见文本中
    expect(screen.queryByText('above')).not.toBeInTheDocument()
    expect(screen.queryByText('below')).not.toBeInTheDocument()
  })
})
