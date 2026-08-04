/**
 * 复盘报告面板测试：列表渲染（时间/复盘区间/动作徽标/error 红字）、展开详情
 * （statsJson 统计表格 + reportMd 全文，字段缺失降级）、「立即复盘」成功刷新
 * 与 409/503 的 ApiError.detail 提示、服务端分页、工具调用链内嵌
 * （roundId 非空 lazy 拉取 getRound；空串 = 老报告灰字降级且不拉取）。
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../api/http'
import type { ReviewReport, ReviewReportSummary, RoundDetail, WsMessage } from '../api/types'
import ReviewPanel from '../components/console/ReviewPanel'

const iso = (unixSec: number) => new Date(unixSec * 1000).toISOString()

/**
 * 7 条报告覆盖两页（最新在前）：id7 失败红字行；id6 改策略（统计三键齐全，
 * roundId 非空，演示工具链内嵌）；id5~1 未调整（statsJson 仅 total_pnl，roundId 空串降级）。
 */
const REPORTS: ReviewReportSummary[] = [
  {
    id: 7,
    periodStart: iso(1784505600),
    periodEnd: iso(1784592000),
    statsJson: '{}',
    reportMd: '',
    strategyAction: 'none',
    newVersionId: null,
    error: 'LLM 响应超时',
    roundId: '',
    time: iso(1784600000),
  },
  {
    id: 6,
    periodStart: iso(1784419200),
    periodEnd: iso(1784505600),
    statsJson: '{"close_count":5,"total_pnl":"-32.10","win_rate":"0.4000","profit_factor":"0.5847"}',
    reportMd: '# 复盘报告\n\n本区间亏损。',
    strategyAction: 'rewrite',
    newVersionId: 3,
    error: '',
    roundId: 'rvw-round-6',
    time: iso(1784510000),
  },
  ...Array.from({ length: 5 }, (_, i) => {
    const id = 5 - i
    return {
      id,
      periodStart: iso(1784332800 - i * 86400),
      periodEnd: iso(1784419200 - i * 86400),
      statsJson: '{"close_count":3,"total_pnl":"18.40"}',
      reportMd: `第 ${id} 份报告：无需调整。`,
      strategyAction: 'none' as const,
      newVersionId: null,
      error: '',
      roundId: '',
      time: iso(1784420000 - i * 3600),
    }
  }),
]

/** id6 关联的复盘审计轮详情：2 条工具调用 + Anthropic 原生格式 llm_raw */
const ROUND_DETAIL: RoundDetail = {
  round_id: 'rvw-round-6',
  prompt_snapshot: 'prompt 快照',
  llm_raw: JSON.stringify({
    role: 'assistant',
    content: [
      { type: 'text', text: '先拉取区间平仓统计，再核对当前策略。' },
      { type: 'tool_use', id: 'toolu_rvw_1', name: 'get_review_stats', input: { hours: 24 } },
    ],
  }),
  tool_calls: [
    {
      seq: 1,
      tool: 'get_review_stats',
      args: { hours: 24 },
      risk_verdict: '',
      risk_reason: '',
      result: '{"close_count":5,"total_pnl":"-32.10"}',
      duration_ms: 8,
    },
    {
      seq: 2,
      tool: 'get_strategy',
      args: {},
      risk_verdict: '',
      risk_reason: '',
      result: '策略书原文',
      duration_ms: 5,
    },
  ],
  strategyMd5: '',
}

const holder = vi.hoisted(() => ({
  getReviewReports: vi.fn(),
  getReviewReport: vi.fn(),
  getRound: vi.fn(),
  runReview: vi.fn(),
  getReviewLive: vi.fn(),
  lastMessage: null as WsMessage | null,
}))
vi.mock('../api', () => ({
  api: {
    getReviewReports: (offset: number, limit: number) => holder.getReviewReports(offset, limit),
    getReviewReport: (id: number) => holder.getReviewReport(id),
    getRound: (roundId: string) => holder.getRound(roundId),
    runReview: () => holder.runReview(),
    getReviewLive: () => holder.getReviewLive(),
  },
}))

// ReviewLiveStrip 经 useWs 订阅复盘事件；lastMessage 经 holder 可控派发（默认 null 无消息，进度条隐藏）
vi.mock('../hooks/useWs', () => ({
  useWs: () => ({ connected: true, lastMessage: holder.lastMessage }),
}))

beforeEach(() => {
  vi.clearAllMocks()
  holder.lastMessage = null
  // 默认无进行中复盘轮：进度条不渲染
  holder.getReviewLive.mockResolvedValue({ round: null, tool_calls: [] })
  holder.getReviewReports.mockImplementation((offset: number, limit: number) =>
    Promise.resolve({ items: REPORTS.slice(offset, offset + limit), total: REPORTS.length }),
  )
  // 详情在列表基础上追加「完整版追加段落」，验证展开时 lazy 拉取的是全文而非列表截断
  holder.getReviewReport.mockImplementation((id: number): Promise<ReviewReport> => {
    const found = REPORTS.find((r) => r.id === id)
    if (!found) return Promise.reject(new ApiError(404, `复盘报告不存在: ${id}`))
    const fullMd = found.reportMd === '' ? '' : `${found.reportMd}\n\n完整版追加段落。`
    return Promise.resolve({ ...found, reportMd: fullMd })
  })
  holder.runReview.mockImplementation(() =>
    Promise.resolve({
      started: true,
      ok: true,
      reportId: 8,
      roundId: 'rv-8',
      strategyAction: 'none',
      newVersionId: null,
      error: '',
    }),
  )
  // 默认给任意 roundId 返回同一份审计详情（id6 展开即触发）
  holder.getRound.mockResolvedValue(ROUND_DETAIL)
})

describe('ReviewPanel(复盘报告)', () => {
  it('列表渲染：error 红字行 / 改策略徽标 / 未调整徽标 / 复盘区间 / 分页摘要', async () => {
    render(<ReviewPanel />)

    expect(await screen.findByText('复盘失败：LLM 响应超时')).toBeInTheDocument()
    expect(screen.getByText('改策略 → v3')).toBeInTheDocument()
    expect(screen.getAllByText('未调整').length).toBe(4) // id7/5/4/3 四条 none（失败行同样带动作徽标）
    expect(screen.getAllByText(/区间 .+ ~ .+/).length).toBe(5)
    expect(screen.getByText('第 1/2 页 · 共 7 条复盘')).toBeInTheDocument()
    expect(holder.getReviewReports).toHaveBeenCalledWith(0, 5)
  })

  it('点击展开：lazy 拉取全文，展示统计表格与 reportMd 完整内容', async () => {
    render(<ReviewPanel />)
    const preview = await screen.findByText(/本区间亏损。/)

    fireEvent.click(preview)
    await waitFor(() => expect(holder.getReviewReport).toHaveBeenCalledWith(6))

    // statsJson → 总盈亏/胜率/盈亏比 三行（负盈亏红色口径由 class 保证，此处断文本）
    expect(await screen.findByText('总盈亏')).toBeInTheDocument()
    expect(screen.getByText('-32.10')).toBeInTheDocument()
    expect(screen.getByText('胜率')).toBeInTheDocument()
    expect(screen.getByText('40.00%')).toBeInTheDocument()
    expect(screen.getByText('盈亏比')).toBeInTheDocument()
    expect(screen.getByText('0.58')).toBeInTheDocument()
    // 全文（列表预览不含追加段落）
    expect(await screen.findByText(/完整版追加段落。/)).toBeInTheDocument()
  })

  it('statsJson 为空对象时降级：展开不渲染统计表格行', async () => {
    render(<ReviewPanel />)
    const errorRow = await screen.findByText('复盘失败：LLM 响应超时')

    fireEvent.click(errorRow)
    await waitFor(() => expect(holder.getReviewReport).toHaveBeenCalledWith(7))
    await waitFor(() => expect(screen.queryByText('报告全文加载中…')).not.toBeInTheDocument())

    expect(screen.queryByText('总盈亏')).not.toBeInTheDocument()
    expect(screen.queryByText('胜率')).not.toBeInTheDocument()
    expect(screen.queryByText('盈亏比')).not.toBeInTheDocument()
  })

  it('工具链内嵌：roundId 非空的报告展开后 lazy 拉取 getRound 并展示工具调用详情', async () => {
    render(<ReviewPanel />)
    const preview = await screen.findByText(/本区间亏损。/)

    fireEvent.click(preview)
    await waitFor(() => expect(holder.getRound).toHaveBeenCalledWith('rvw-round-6'))

    expect(await screen.findByText('工具调用详情 · tool_calls（2 步）')).toBeInTheDocument()

    // 默认收起（设计规格）：工具步骤默认不在文档中，点击标题展开后可见
    // （锚点用 seq N 而非工具名：ConversationThread 的 details 内容始终在 DOM，会命中工具名）
    expect(screen.queryByText('seq 1')).not.toBeInTheDocument()
    fireEvent.click(screen.getByText('工具调用详情 · tool_calls（2 步）'))
    expect(await screen.findByText('seq 1')).toBeInTheDocument()
  })

  it('工具链降级：roundId 空串的老报告展开后灰字提示，且不拉取 getRound', async () => {
    render(<ReviewPanel />)
    const preview = await screen.findByText(/第 5 份报告/)

    fireEvent.click(preview)
    await waitFor(() => expect(holder.getReviewReport).toHaveBeenCalledWith(5))

    expect(await screen.findByText('该报告早于工具链留痕功能，无工具调用记录')).toBeInTheDocument()
    expect(holder.getRound).not.toHaveBeenCalled()
  })

  it('工具链 lazy：不展开任何报告时不拉取 getRound', async () => {
    render(<ReviewPanel />)
    await screen.findByText(/第 5 份报告/)

    expect(holder.getRound).not.toHaveBeenCalled()
  })

  it('立即复盘成功：提示成功并刷新列表', async () => {
    render(<ReviewPanel />)
    await screen.findByText(/第 5 份报告/)
    const callsBefore = holder.getReviewReports.mock.calls.length

    fireEvent.click(screen.getByRole('button', { name: '立即复盘' }))

    expect(await screen.findByText('复盘已完成，最新报告已入列')).toBeInTheDocument()
    expect(holder.runReview).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(holder.getReviewReports).toHaveBeenCalledTimes(callsBefore + 1))
  })

  it('立即复盘 409：展示 ApiError.detail（复盘进行中）', async () => {
    holder.runReview.mockRejectedValueOnce(new ApiError(409, '复盘进行中'))
    render(<ReviewPanel />)
    await screen.findByText(/第 5 份报告/)

    fireEvent.click(screen.getByRole('button', { name: '立即复盘' }))

    expect(await screen.findByText('复盘进行中')).toBeInTheDocument()
  })

  it('立即复盘 503：展示 ApiError.detail（LLM 未配置）', async () => {
    holder.runReview.mockRejectedValueOnce(new ApiError(503, 'LLM 未配置'))
    render(<ReviewPanel />)
    await screen.findByText(/第 5 份报告/)

    fireEvent.click(screen.getByRole('button', { name: '立即复盘' }))

    expect(await screen.findByText('LLM 未配置')).toBeInTheDocument()
  })

  it('立即复盘返回 started=true 但 ok=false（复盘执行失败）：红色提示失败原因且仍刷新列表', async () => {
    // 后端路由仅把「LLM 未配置」「复盘进行中」映 503/409，其余失败以 200 返回 ok=false（失败报告已落库）
    holder.runReview.mockResolvedValueOnce({
      started: true,
      ok: false,
      reportId: 8,
      roundId: 'rv-8',
      strategyAction: 'none',
      newVersionId: null,
      error: 'LLM 调用异常',
    })
    render(<ReviewPanel />)
    await screen.findByText(/第 5 份报告/)
    const callsBefore = holder.getReviewReports.mock.calls.length

    fireEvent.click(screen.getByRole('button', { name: '立即复盘' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('复盘失败：LLM 调用异常')
    await waitFor(() => expect(holder.getReviewReports).toHaveBeenCalledTimes(callsBefore + 1))
  })

  it('分页：下一页拉取 offset=5 并渲染第二页内容', async () => {
    render(<ReviewPanel />)
    await screen.findByText(/第 5 份报告/)

    fireEvent.click(screen.getByRole('button', { name: '下一页' }))

    await waitFor(() => expect(holder.getReviewReports).toHaveBeenLastCalledWith(5, 5))
    expect(await screen.findByText('第 2 份报告：无需调整。')).toBeInTheDocument()
    expect(screen.getByText('第 1 份报告：无需调整。')).toBeInTheDocument()
    expect(screen.getByText('第 2/2 页 · 共 7 条复盘')).toBeInTheDocument()
  })

  it('进度条联动：review_round_start 出现进度条，review_round 结束后消失并自动刷新报告列表', async () => {
    const { rerender } = render(<ReviewPanel />)
    await screen.findByText(/第 5 份报告/)
    expect(screen.queryByTestId('review-live-strip')).not.toBeInTheDocument()

    // 注入复盘开始事件：进度条出现（轮询数据源返回进行中轮与 1 条 {text} 包装的工具调用）
    holder.getReviewLive.mockResolvedValue({
      round: {
        round_id: 'rv-live',
        wake_source: 'review',
        prompt_md5: 'md5',
        prompt_snapshot: 'prompt',
        context_snapshot: 'ctx',
        llm_raw: '',
        strategy_md5: 's-md5',
        started_at: Math.floor(Date.now() / 1000) - 5,
        ended_at: null,
        error: '',
      },
      tool_calls: [
        {
          seq: 1,
          tool: 'get_review_stats',
          args: { interval_days: 1 },
          risk_verdict: '',
          risk_reason: '',
          result: { text: '概览' },
          duration_ms: 12,
        },
      ],
    })
    holder.lastMessage = { type: 'review_round_start', data: { round_id: 'rv-live' } }
    rerender(<ReviewPanel />)
    expect(await screen.findByTestId('review-live-strip')).toBeInTheDocument()

    // 注入复盘结束事件：进度条消失，onFinished（即 refreshToLatest）触发报告列表自动刷新
    const callsBefore = holder.getReviewReports.mock.calls.length
    holder.lastMessage = { type: 'review_round', data: { round_id: 'rv-live', ok: true } }
    rerender(<ReviewPanel />)

    await waitFor(() => expect(screen.queryByTestId('review-live-strip')).not.toBeInTheDocument())
    await waitFor(() => expect(holder.getReviewReports).toHaveBeenCalledTimes(callsBefore + 1))
  })
})
