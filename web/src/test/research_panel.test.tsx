/** 研报面板测试：列表渲染（失败红字/逐标的标签/分页摘要）、展开详情（逐标的+因果链+工具链）、
 *  失败卡片不可展开、手动触发点火（绿提示、按钮立即恢复、不主动刷新、research-round-ignite 事件激活状态条）、
 *  409 按成功样式提示并广播 research-round-catchup 让状态条补漏激活、503 红字 ApiError.detail、
 *  状态条结束后清提示并自动刷新列表、服务端分页。 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../api/http'
import type {
  ResearchAssetDetail,
  ResearchAssetSummary,
  ResearchLiveRound,
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
  context_snapshot: '研报简报',
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

/** 进行中的研报轮（/api/research/live 形状）：409 catchup 补漏联动用例用（started_at 贴近当前，避免触发僵尸轮防线）。 */
const LIVE_ROUND: ResearchLiveRound = {
  round_id: 'rs-busy',
  wake_source: 'research',
  prompt_md5: 'md5',
  prompt_snapshot: 'prompt',
  context_snapshot: 'ctx',
  llm_raw: '',
  started_at: Math.floor(Date.now() / 1000) - 10,
  ended_at: null,
  error: '',
}

const holder = vi.hoisted(() => ({
  getResearchReports: vi.fn(),
  getResearchReport: vi.fn(),
  getRound: vi.fn(),
  runResearch: vi.fn(),
  getResearchLive: vi.fn(),
  lastMessage: null as WsMessage | null,
}))

vi.mock('../api', async () => {
  // 面板 runNow 的 catch 分支做 instanceof ApiError：mock 必须透出真实类，测试经 ../api/http 构造的实例才能命中
  const { ApiError } = await import('../api/http')
  return {
    api: {
      getResearchReports: (offset: number, limit: number) => holder.getResearchReports(offset, limit),
      getResearchReport: (id: number) => holder.getResearchReport(id),
      getRound: (roundId: string) => holder.getRound(roundId),
      runResearch: (reportType?: string, hours?: number) => holder.runResearch(reportType, hours),
      getResearchLive: () => holder.getResearchLive(),
    },
    ApiError,
  }
})

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
    reportType: 'manual',
    hours: 24,
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

  it('生成研报点火成功：提示已启动、按钮立即恢复且不主动刷新列表', async () => {
    render(<ResearchPanel />)
    await screen.findByText('亚盘 BTC 获得宏观与技术共振。')
    const before = holder.getResearchReports.mock.calls.length
    fireEvent.click(screen.getByRole('button', { name: '生成研报' }))
    expect(await screen.findByText('研报已启动，进度见下方状态条')).toBeInTheDocument()
    expect(holder.runResearch).toHaveBeenCalledWith('manual', 24)
    // 点火即返回：按钮立即恢复；列表不随点火刷新（结果经状态条 onFinished 刷新）
    expect(screen.getByRole('button', { name: '生成研报' })).toBeEnabled()
    expect(holder.getResearchReports).toHaveBeenCalledTimes(before)
  })

  it('生成研报 409（进行中）：按成功样式提示 ApiError.detail，不用错误红', async () => {
    holder.runResearch.mockRejectedValueOnce(new ApiError(409, '研报生成中'))
    render(<ResearchPanel />)
    fireEvent.click(await screen.findByRole('button', { name: '生成研报' }))
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('研报生成中')
    expect(alert.className).toContain('emerald')
    expect(alert.className).not.toContain('rose')
  })

  it('生成研报 409 且状态条未激活：广播 catchup 事件让状态条经补漏找回进行中轮', async () => {
    holder.runResearch.mockRejectedValueOnce(new ApiError(409, '研报生成中'))
    render(<ResearchPanel />)
    await screen.findByText('亚盘 BTC 获得宏观与技术共振。')
    expect(screen.queryByTestId('research-live-strip')).not.toBeInTheDocument()

    // 他处（别的标签页/自动调度）已点火：/live 可见进行中轮；本页 WS 断线收不到 start 事件
    holder.getResearchLive.mockResolvedValue({ round: LIVE_ROUND, tool_calls: [] })
    fireEvent.click(screen.getByRole('button', { name: '生成研报' }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('研报生成中')
    expect(alert.className).toContain('emerald')
    expect(await screen.findByTestId('research-live-strip')).toBeInTheDocument()
  })

  it('生成研报 503：红字展示 ApiError.detail（LLM 未配置）', async () => {
    holder.runResearch.mockRejectedValueOnce(new ApiError(503, 'LLM 未配置'))
    render(<ResearchPanel />)
    fireEvent.click(await screen.findByRole('button', { name: '生成研报' }))
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('LLM 未配置')
    expect(alert.className).toContain('rose')
  })

  it('进度条联动：点火后状态条不经 WS 即激活，WS 轮结束事件后状态条消失、提示清空并自动刷新列表', async () => {
    const { rerender } = render(<ResearchPanel />)
    await screen.findByText('亚盘 BTC 获得宏观与技术共振。')
    expect(screen.queryByTestId('research-live-strip')).not.toBeInTheDocument()

    // 点火：绿提示出现 + 状态条经 research-round-ignite 事件激活（覆盖 WS 断线窗口内点火场景）
    fireEvent.click(screen.getByRole('button', { name: '生成研报' }))
    expect(await screen.findByText('研报已启动，进度见下方状态条')).toBeInTheDocument()
    expect(await screen.findByTestId('research-live-strip')).toBeInTheDocument()

    // WS 注入研报结束事件：状态条消失，onFinished（即 refreshToLatest）清提示并自动刷新研报列表
    const callsBefore = holder.getResearchReports.mock.calls.length
    holder.lastMessage = { type: 'research_round', data: { round_id: 'rs-live', ok: true } }
    rerender(<ResearchPanel />)

    await waitFor(() => expect(screen.queryByTestId('research-live-strip')).not.toBeInTheDocument())
    await waitFor(() => expect(holder.getResearchReports).toHaveBeenCalledTimes(callsBefore + 1))
    expect(screen.queryByText('研报已启动，进度见下方状态条')).not.toBeInTheDocument()
  })

  it('分页到第二页按 offset=5 拉取', async () => {
    render(<ResearchPanel />)
    fireEvent.click(await screen.findByRole('button', { name: '下一页' }))
    await waitFor(() => expect(holder.getResearchReports).toHaveBeenLastCalledWith(5, 5))
    expect(await screen.findByText('第 1 份研报')).toBeInTheDocument()
  })
})
