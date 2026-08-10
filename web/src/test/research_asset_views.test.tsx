import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { ResearchAssetSummary } from '../api/types'
import { ResearchAssetBadges, ResearchAssetDetails } from '../components/console/ResearchAssetViews'

const asset: ResearchAssetSummary = {
  contract: 'BTC_USDT',
  direction: '偏多',
  confidence: '高',
  horizon: '3日',
  marketRegime: '上涨趋势',
  technicalConfirmation: '确认',
  basisType: '混合',
  dataStatus: '完整',
}

describe('逐标的研报展示', () => {
  it('列表显示每个合约的方向标签', () => {
    render(<ResearchAssetBadges assets={[asset, { ...asset, contract: 'ETH_USDT', direction: '中性' }]} />)
    expect(screen.getByText('BTC_USDT · 偏多')).toBeInTheDocument()
    expect(screen.getByText('ETH_USDT · 中性')).toBeInTheDocument()
  })

  it('详情显示结构、依据、技术确认、证据、风险和缺失数据状态', () => {
    render(
      <ResearchAssetDetails
        summary="市场分化"
        crossMarketView="BTC 强于 ETH"
        globalRisks={['宏观波动']}
        assets={[{
          ...asset,
          evidence: ['放量增仓（4h）'],
          risks: ['资金费率偏高'],
          narrative: 'BTC 结构获得催化验证',
          verifyResult: '',
          time: new Date(0).toISOString(),
        }, {
          ...asset,
          contract: 'ETH_USDT',
          direction: '中性',
          dataStatus: '不可用',
          technicalConfirmation: '不可用',
          evidence: [],
          risks: [],
          narrative: '数据不足，维持中性。',
          verifyResult: '',
          time: new Date(0).toISOString(),
        }]}
      />,
    )
    expect(screen.getByText('跨标的观察：BTC 强于 ETH')).toBeInTheDocument()
    expect(screen.getByText(/结构：上涨趋势 · 依据：混合 · 技术确认：确认/)).toBeInTheDocument()
    expect(screen.getByText(/放量增仓（4h）/)).toBeInTheDocument()
    expect(screen.getByText(/资金费率偏高/)).toBeInTheDocument()
    expect(screen.getByText(/数据：不可用/)).toBeInTheDocument()
  })
})
