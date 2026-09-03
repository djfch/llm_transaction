import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { ResearchAssetSummary, ResearchRereviewAck } from '../api/types'
import { ResearchAssetBadges, ResearchAssetDetails } from '../components/console/ResearchAssetViews'

// mock ../api：仅替换 requestResearchRereview（R5-2 授权登记），ApiError 用同名最小类保持 instanceof 判定
const holder = vi.hoisted(() => ({
  request: vi.fn() as ReturnType<
    typeof vi.fn<(r: number, c: string, reason: string) => Promise<ResearchRereviewAck>>
  >,
}))
vi.mock('../api', () => ({
  api: {
    requestResearchRereview: (r: number, c: string, reason: string) => holder.request(r, c, reason),
  },
  ApiError: class ApiError extends Error {
    status: number
    detail: string
    constructor(status: number, detail: string) {
      super(detail)
      this.status = status
      this.detail = detail
    }
  },
}))
import { ApiError } from '../api'

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
        reportId={1}
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
          price_start_at: '2026-08-06T17:00:00+00:00', price_end_at: '2026-08-06T18:30:00+00:00',
          start_price: 67400, end_price: 70800, return_pct: 5.04,
          high: 71500, max_up_pct: 6.1, low: 66600, max_down_pct: -1.2,
        },
        createdAt: '2026-08-07T01:05:00.000Z',
      }],
    }
    render(
      <ResearchAssetDetails
        reportId={1}
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
    // 客观结果摘要（与后端 _format_outcome 同口径，数据状态只保留中文；含首/末价格时点）
    expect(screen.getByText(/客观结果：数据状态 完整（K线 6\/6） \| 起价 67400 → 止价 70800 \| 涨跌 5.04%/)).toBeInTheDocument()
    expect(screen.getByText(/价格时点 .+ → .+/)).toBeInTheDocument()
    // ETH 无复盘：不出现第二个复盘块
    expect(screen.getAllByText(/复盘 · /)).toHaveLength(1)
  })

  it('复盘客观结果无价格数据时只呈现状态与说明', () => {
    render(
      <ResearchAssetDetails
        reportId={1}
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

  it('复盘客观结果止价缺失时只呈现起价与区间高低', () => {
    render(
      <ResearchAssetDetails
        reportId={1}
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
            id: 4,
            reviewReportId: 9,
            directionRelation: '',
            directionReason: '',
            reasoningQuality: '',
            reasoningReview: '',
            evidenceReviews: [],
            confidenceAssessment: '',
            confidenceReason: '',
            improvementAdvice: '',
            outcome: {
              data_status: 'partial', candles_actual: 1, candles_expected: 96,
              start_price: 67400, end_price: null, return_pct: null,
              high: 68000, max_up_pct: 0.89, low: 67000, max_down_pct: -0.59,
              error: '窗口末端无完整 K 线，止价缺失',
            },
            createdAt: '2026-08-07T01:05:00.000Z',
          }],
        }]}
      />,
    )
    // 止价/涨跌缺失：渲染 error 说明而非「止价 null」，区间高低仍呈现
    expect(screen.getByText(/起价 67400 → 窗口末端无完整 K 线，止价缺失/)).toBeInTheDocument()
    expect(screen.getByText(/区间最高 68000（0.89%）/)).toBeInTheDocument()
    expect(screen.queryByText(/涨跌 null/)).not.toBeInTheDocument()
  })
})

/** 构造一条最小复盘记录（字段全覆盖，测试按需覆写） */
function reviewOf(patch: Partial<import('../api/types').ResearchReviewItem>) {
  return {
    id: 2,
    reviewReportId: 7,
    directionRelation: '',
    directionReason: '',
    reasoningQuality: '',
    reasoningReview: '',
    evidenceReviews: [],
    confidenceAssessment: '',
    confidenceReason: '',
    improvementAdvice: '',
    outcome: {},
    createdAt: '2026-08-07T01:05:00.000Z',
    ...patch,
  }
}

/** 构造带复盘记录的逐标的详情（申请重评入口的挂载前提） */
function reviewedAsset(reviews: ReturnType<typeof reviewOf>[]) {
  return {
    ...asset,
    evidence: [],
    risks: [],
    narrative: '',
    time: new Date(0).toISOString(),
    researchReviews: reviews,
  }
}

describe('人工授权重评（R5-2）', () => {
  it('manual 复盘记录显示「人工重评」徽标与重评理由；auto 记录不显示', () => {
    render(
      <ResearchAssetDetails
        reportId={1}
        summary=""
        crossMarketView=""
        globalRisks={[]}
        assets={[
          reviewedAsset([
            reviewOf({ id: 2, reviewKind: 'auto' }),
            reviewOf({
              id: 3,
              reviewKind: 'manual',
              rereviewReason: '原复盘把震荡误判为背离',
            }),
          ]),
        ]}
      />,
    )
    expect(screen.getAllByText('人工重评')).toHaveLength(1) // 仅 manual 卡有徽标
    expect(screen.getByText('重评理由：')).toBeInTheDocument()
    expect(screen.getByText('原复盘把震荡误判为背离')).toBeInTheDocument()
  })

  it('已复盘标的显示申请重评入口；登记成功提示授权编号，幂等命中给出已有授权提示', async () => {
    holder.request.mockResolvedValueOnce({ id: 5, reused: false })
    render(
      <ResearchAssetDetails
        reportId={9}
        summary=""
        crossMarketView=""
        globalRisks={[]}
        assets={[reviewedAsset([reviewOf({})])]}
      />,
    )
    fireEvent.click(screen.getByRole('button', { name: '申请重评' }))
    // 空理由时确认按钮不可用（后端 422 的前端前置约束）
    expect(screen.getByRole('button', { name: '确认登记' })).toBeDisabled()
    fireEvent.change(screen.getByPlaceholderText(/重评理由/), {
      target: { value: '  原复盘误判  ' },
    })
    fireEvent.click(screen.getByRole('button', { name: '确认登记' }))
    expect(holder.request).toHaveBeenCalledWith(9, 'BTC_USDT', '原复盘误判') // 理由 trim 后上送
    expect(await screen.findByText('已登记重评授权（授权#5），下一轮复盘生效')).toBeInTheDocument()

    // 再次发起：服务端幂等命中既有授权 → 提示无需重复登记
    holder.request.mockResolvedValueOnce({ id: 5, reused: true })
    fireEvent.click(screen.getByRole('button', { name: '申请重评' }))
    fireEvent.change(screen.getByPlaceholderText(/重评理由/), { target: { value: '再评一次' } })
    fireEvent.click(screen.getByRole('button', { name: '确认登记' }))
    expect(
      await screen.findByText('该标的已有待处理的重评授权（授权#5），无需重复登记'),
    ).toBeInTheDocument()
  })

  it('登记失败显示服务端 detail；未复盘标的不显示入口', async () => {
    holder.request.mockRejectedValueOnce(new ApiError(409, '该结论尚未被正式复盘'))
    render(
      <ResearchAssetDetails
        reportId={1}
        summary=""
        crossMarketView=""
        globalRisks={[]}
        assets={[
          reviewedAsset([reviewOf({})]),
          { ...asset, contract: 'ETH_USDT', evidence: [], risks: [], narrative: '', time: new Date(0).toISOString(), researchReviews: [] },
        ]}
      />,
    )
    expect(screen.getAllByRole('button', { name: '申请重评' })).toHaveLength(1) // ETH 无复盘无入口
    fireEvent.click(screen.getByRole('button', { name: '申请重评' }))
    fireEvent.change(screen.getByPlaceholderText(/重评理由/), { target: { value: '复核' } })
    fireEvent.click(screen.getByRole('button', { name: '确认登记' }))
    expect(await screen.findByText('该结论尚未被正式复盘')).toBeInTheDocument()
  })
})
