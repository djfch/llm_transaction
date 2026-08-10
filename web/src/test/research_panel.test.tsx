/** 研报面板只展示当前逐标的结构，并覆盖成功、失败、分页和手动触发。 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../api/http'
import type {
  ResearchAssetDetail,
  ResearchAssetSummary,
  ResearchReportDetail,
  ResearchReportSummary,
  RoundDetail,
  WsMessage,
} from '../api/types'
import ResearchPanel from '../components/console/ResearchPanel'

const iso = (unixSec: number) => new Date(unixSec * 1000).toISOString()

function asset(contract: string, direction = '中性'): ResearchAssetSummary {
  return {
    contract,
    direction,
    confidence: direction === '偏多' ? '高' : '低',
    horizon: '3日',
    marketRegime: direction === '偏多' ? '上涨趋势' : '震荡',
    technicalConfirmation: direction === '偏多' ? '确认' : '中性',
    basisType: direction === '偏多' ? '混合' : '结构延续',
    dataStatus: '完整',
  }
}

function report(id: number, summary: string): ResearchReportSummary {
  return {
    id,
    reportType: 'manual',
    schemaVersion: 2,
    summary,
    crossMarketView: '',
    globalRisks: [],
    assetViews: [asset('BTC_USDT')],
    error: '',
    roundId: '',
    time: iso(1784420000 - id * 3600),
  }
}

const REPORTS: ResearchReportSummary[] = [
  {
    id: 7,
    reportType: 'manual',
    schemaVersion: 2,
    summary: '',
    crossMarketView: '',
    globalRisks: [],
    assetViews: [],
    error: 'LLM 响应超时',
    roundId: '',
    time: iso(1784600000),
  },
  {
    id: 6,
    reportType: 'asia_open',
    schemaVersion: 2,
    summary: '亚盘 BTC 获得宏观与技术共振。',
    crossMarketView: 'BTC 强于 ETH',
    globalRisks: ['美联储鹰派讲话'],
    assetViews: [asset('BTC_USDT', '偏多')],
    error: '',
    roundId: 'rs-round-6',
    time: iso(1784510000),
  },
  ...Array.from({ length: 5 }, (_, index) => report(5 - index, '第 ' + (5 - index) + ' 份研报')),
]

function detailAsset(summary: ResearchAssetSummary): ResearchAssetDetail {
  return {
    ...summary,
    evidence: ['放量增仓（4h）'],
    risks: ['资金费率偏高'],
    narrative: 'BTC 逐标的研判',
    verifyResult: '',
    time: iso(1784510000),
  }
}

const ROUND_DETAIL: RoundDetail = {
  round_id: 'rs-round-6',
  prompt_snapshot: 'prompt 快照',
  llm_raw: '',
  tool_calls: [{
    seq: 1,
    tool: 'get_research_market_data',
    args: { contract: 'BTC_USDT' },
    risk_verdict: '',
    risk_reason: '',
    result: '{"contract":"BTC_USDT"}',
    duration_ms: 8,
  }],
  strategyMd5: '',
}

const holder = vi.hoisted(() => ({
  getResearchReports: vi.fn(),
  getResearchReport: vi.fn(),
  getRound: vi.fn(),
  runResearch: vi.fn(),
  getResearchLive: vi.fn(),
  lastMessage: null as WsMessage | null,
}))

vi.mock('../api', () => ({
  api: {
    getResearchReports: (offset: number, limit: number) => holder.getResearchReports(offset, limit),
    getResearchReport: (id: number) => holder.getResearchReport(id),
    getRound: (roundId: string) => holder.getRound(roundId),
    runResearch: (reportType?: string, hours?: number) => holder.runResearch(reportType, hours),
    getResearchLive: () => holder.getResearchLive(),
  },
}))

vi.mock('../hooks/useWs', () => ({
  useWs: () => ({ connected: true, lastMessage: holder.lastMessage }),
}))

beforeEach(() => {
  vi.clearAllMocks()
  holder.lastMessage = null
  holder.getResearchLive.mockResolvedValue({ round: null, tool_calls: [] })
  holder.getResearchReports.mockImplementation((offset: number, limit: number) =>
    Promise.resolve({ items: REPORTS.slice(offset, offset + limit), total: REPORTS.length }),
  )
  holder.getResearchReport.mockImplementation((id: number): Promise<ResearchReportDetail> => {
    const found = REPORTS.find((item) => item.id === id)
    if (!found) return Promise.reject(new ApiError(404, '研报不存在: ' + id))
    return Promise.resolve({
      ...found,
      assetViews: found.assetViews.map(detailAsset),
      causalLinks: id === 6 ? [{
        id: 1,
        reportId: 6,
        chain: [
          { node: 'CPI 低于预期', kind: '事件', timeline_id: 1287 },
          { node: 'BTC 风险偏好修复', kind: '标的结论' },
        ],
        confidence: 0.72,
        evidence: ['金十日历'],
        status: 'verified',
        brokenAt: null,
        topic: 'CPI',
        supersedesId: null,
        awaitVerification: false,
        time: iso(1784510000),
      }] : [],
    })
  })
  holder.getRound.mockResolvedValue(ROUND_DETAIL)
  holder.runResearch.mockResolvedValue({
    started: true,
    ok: true,
    reportId: 8,
    roundId: 'rs-8',
    assetCount: 2,
    error: '',
  })
})

describe('ResearchPanel(研报面板)', () => {
  it('列表显示失败信息、成功逐合约标签和分页摘要', async () => {
    render(<ResearchPanel />)
    expect(await screen.findByText('研报失败：LLM 响应超时')).toBeInTheDocument()
    expect(screen.getByText('亚盘')).toBeInTheDocument()
    expect(screen.getByText('BTC_USDT · 偏多')).toBeInTheDocument()
    expect(screen.getByText('亚盘 BTC 获得宏观与技术共振。')).toBeInTheDocument()
    expect(screen.queryByText('旧版')).not.toBeInTheDocument()
    expect(screen.getByText('第 1/2 页 · 共 7 条研报')).toBeInTheDocument()
  })

  it('成功卡片展开逐标的详情、因果链和工具调用链', async () => {
    render(<ResearchPanel />)
    fireEvent.click(await screen.findByText('亚盘 BTC 获得宏观与技术共振。'))
    expect(await screen.findByText('跨标的观察：BTC 强于 ETH')).toBeInTheDocument()
    expect(screen.getByText('BTC 逐标的研判')).toBeInTheDocument()
    expect(screen.getByText(/放量增仓（4h）/)).toBeInTheDocument()
    expect(screen.getByText(/资金费率偏高/)).toBeInTheDocument()
    expect(screen.getByText('CPI 低于预期')).toBeInTheDocument()
    await waitFor(() => expect(holder.getRound).toHaveBeenCalledWith('rs-round-6'))
  })

  it('失败卡片不可展开且不读取详情', async () => {
    render(<ResearchPanel />)
    const error = await screen.findByText('研报失败：LLM 响应超时')
    const button = error.closest('button')
    expect(button).toBeDisabled()
    if (button) fireEvent.click(button)
    expect(holder.getResearchReport).not.toHaveBeenCalledWith(7)
  })

  it('生成研报成功后刷新第一页', async () => {
    render(<ResearchPanel />)
    await screen.findByText('亚盘 BTC 获得宏观与技术共振。')
    const before = holder.getResearchReports.mock.calls.length
    fireEvent.click(screen.getByRole('button', { name: '生成研报' }))
    expect(await screen.findByText('研报已生成，最新研报已入列')).toBeInTheDocument()
    expect(holder.runResearch).toHaveBeenCalledWith('manual', 24)
    await waitFor(() => expect(holder.getResearchReports).toHaveBeenCalledTimes(before + 1))
  })

  it('生成研报错误显示后端 detail', async () => {
    holder.runResearch.mockRejectedValueOnce(new ApiError(409, '研报生成中'))
    render(<ResearchPanel />)
    fireEvent.click(await screen.findByRole('button', { name: '生成研报' }))
    expect(await screen.findByText('研报生成中')).toBeInTheDocument()
  })

  it('分页到第二页按 offset=5 拉取', async () => {
    render(<ResearchPanel />)
    fireEvent.click(await screen.findByRole('button', { name: '下一页' }))
    await waitFor(() => expect(holder.getResearchReports).toHaveBeenLastCalledWith(5, 5))
    expect(await screen.findByText('第 1 份研报')).toBeInTheDocument()
  })
})
