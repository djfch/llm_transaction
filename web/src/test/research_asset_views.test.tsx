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

  it('详情渲染研报复盘块：三维枚举评价与理由、逐条依据评价与客观结果摘要；空数组不渲染', () => {
    const reviewed = {
      ...asset,
      evidence: ['放量增仓（4h）'],
      risks: [],
      narrative: '',
      time: new Date(0).toISOString(),
      researchReviews: [{
        id: 2,
        reviewReportId: 7,
        directionRelation: 'realized',
        directionReason: '窗口内上行，方向一致',
        reasoningQuality: 'sound',
        reasoningReview: '因果链成立',
        evidenceReviews: [{
          evidenceIndex: 0,
          factStatus: 'confirmed',
          reasoningStatus: 'supported',
          explanation: '引用准确',
        }],
        confidenceAssessment: 'appropriate',
        confidenceReason: '与证据强度匹配',
        improvementAdvice: '宏观依据须注明兑现窗口',
        outcome: {
          data_status: 'complete', candles_actual: 6, candles_expected: 6,
          start_price: 67400, end_price: 70800, return_pct: 5.04,
          high: 71500, max_up_pct: 6.1, low: 66600, max_down_pct: -1.2,
        },
        createdAt: '2026-08-07T01:05:00.000Z',
      }],
    }
    render(
      <ResearchAssetDetails
        summary=""
        crossMarketView=""
        globalRisks={[]}
        assets={[reviewed, { ...asset, contract: 'ETH_USDT', evidence: [], risks: [], narrative: '', time: new Date(0).toISOString(), researchReviews: [] }]}
      />,
    )
    // 复盘块头部与三维枚举评价（枚举只显示中文释义，括号内为理由）
    expect(screen.getByText(/复盘报告 #7/)).toBeInTheDocument()
    expect(screen.getByText('兑现（窗口内上行，方向一致）')).toBeInTheDocument()
    expect(screen.getByText('成立（因果链成立）')).toBeInTheDocument()
    expect(screen.getByText('匹配合理（与证据强度匹配）')).toBeInTheDocument()
    expect(screen.getByText('宏观依据须注明兑现窗口')).toBeInTheDocument()
    // 逐条依据评价（序号从 0 开始，含事实/推理双枚举释义）
    expect(screen.getByText(/#0 事实：已证实 · 推理：支撑结论 —— 引用准确/)).toBeInTheDocument()
    // 客观结果摘要（与后端 _format_outcome 同口径，数据状态只保留中文）
    expect(screen.getByText(/客观结果：数据状态 完整（K线 6\/6） \| 起价 67400 → 止价 70800 \| 涨跌 5.04%/)).toBeInTheDocument()
    // ETH 无复盘：不出现第二个复盘块
    expect(screen.getAllByText(/复盘 · /)).toHaveLength(1)
  })

  it('复盘客观结果无价格数据时只呈现状态与说明', () => {
    render(
      <ResearchAssetDetails
        summary=""
        crossMarketView=""
        globalRisks={[]}
        assets={[{
          ...asset,
          evidence: [],
          risks: [],
          narrative: '',
          time: new Date(0).toISOString(),
          researchReviews: [{
            id: 3,
            reviewReportId: 8,
            directionRelation: '',
            directionReason: '',
            reasoningQuality: '',
            reasoningReview: '',
            evidenceReviews: [],
            confidenceAssessment: '',
            confidenceReason: '',
            improvementAdvice: '',
            outcome: { data_status: 'unavailable', error: '窗口内无 K 线' },
            createdAt: '2026-08-07T01:05:00.000Z',
          }],
        }]}
      />,
    )
    expect(screen.getByText(/客观结果：数据状态 不可用（窗口内无 K 线）/)).toBeInTheDocument()
    // 评价全空时不渲染对应标签
    expect(screen.queryByText('方向关系：')).not.toBeInTheDocument()
  })
})
