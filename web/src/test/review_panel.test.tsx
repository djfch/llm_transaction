/**
 * 复盘报告面板测试：列表渲染（时间/复盘区间/动作徽标/error 红字）、展开详情
 * （statsJson 统计表格 + reportMd 全文，字段缺失降级）、「立即复盘」点火提示
 * （点火即返回，review-round-ignite 事件激活状态条，结果经状态条 onFinished 刷新）、
 * 409（成功样式 + 广播 review-round-catchup 让状态条补漏激活）/503（错误红）的 ApiError.detail 提示、
 * 服务端分页、工具调用链内嵌
 * （roundId 非空 lazy 拉取 getRound；空串 = 老报告灰字降级且不拉取）。
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../api/http'
import type { ReviewLiveRound, ReviewReport, ReviewReportSummary, RoundDetail, WsMessage } from '../api/types'
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
    llmCredentialName: '',
    llmProvider: '',
    llmModel: '',
    llmThinkingEffort: '',
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
    llmCredentialName: 'kimi-main',
    llmProvider: 'openai_responses',
    llmModel: 'kimi-k2-thinking',
    llmThinkingEffort: 'high',
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
      llmCredentialName: '',
      llmProvider: '',
      llmModel: '',
      llmThinkingEffort: '',
    }
  }),
]

/** id6 关联的复盘审计轮详情：2 条工具调用 + Anthropic 原生格式 llm_raw */
const ROUND_DETAIL: RoundDetail = {
  round_id: 'rvw-round-6',
  prompt_snapshot: 'prompt 快照',
  context_snapshot: '复盘简报',
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
  llmCredentialName: '',
  llmProvider: '',
  llmModel: '',
  llmThinkingEffort: '',
}

/** 进行中的复盘轮（/api/review/live 形状）：409 catchup 补漏联动用例用（started_at 贴近当前，避免触发僵尸轮防线）。 */
const LIVE_ROUND: ReviewLiveRound = {
  round_id: 'rv-busy',
  wake_source: 'review',
  prompt_md5: 'md5',
  prompt_snapshot: 'prompt',
  context_snapshot: 'ctx',
  llm_raw: '',
  strategy_md5: 's-md5',
  started_at: Math.floor(Date.now() / 1000) - 10,
  ended_at: null,
  error: '',
}

const holder = vi.hoisted(() => ({
  getReviewReports: vi.fn(),
  getReviewReport: vi.fn(),
  getRound: vi.fn(),
  runReview: vi.fn(),
  getReviewLive: vi.fn(),
  lastMessage: null as WsMessage | null,
}))
vi.mock('../api', async () => {
  // 面板 runNow 的 catch 分支做 instanceof ApiError：mock 必须透出真实类，测试经 ../api/http 构造的实例才能命中
  const { ApiError } = await import('../api/http')
  return {
    api: {
      getReviewReports: (offset: number, limit: number) => holder.getReviewReports(offset, limit),
      getReviewReport: (id: number) => holder.getReviewReport(id),
      getRound: (roundId: string) => holder.getRound(roundId),
      runReview: () => holder.runReview(),
      getReviewLive: () => holder.getReviewLive(),
    },
    ApiError,
  }
})

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
      periodStart: 1784505600,
      periodEnd: 1784592000,
      roundId: 'rv-ignite', // 预分配审计轮 ID：与下方联动用例的 WS 轮末事件 round_id 一致
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

  it('模型徽标：带身份的报告行显示模型与思考强度，无身份的老报告不渲染徽标', async () => {
    render(<ReviewPanel />)
    await screen.findByText('复盘失败：LLM 响应超时')

    // 仅 id6（身份齐全）带模型徽标；失败行与 roundId 空串的老报告均不渲染
    expect(screen.getAllByText('kimi-k2-thinking · high')).toHaveLength(1)
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

  it('慢请求回归：展开→请求未返回时收起→再展开→请求完成，最终显示全文而非永远加载中', async () => {
    // 第一次请求挂起（模拟慢请求），完成时机由测试可控
    let resolveFirst: (d: ReviewReport) => void = () => {}
    holder.getReviewReport.mockImplementationOnce(
      () => new Promise<ReviewReport>((resolve) => { resolveFirst = resolve }),
    )
    render(<ReviewPanel />)
    const preview = await screen.findByText(/本区间亏损。/)

    fireEvent.click(preview) // 展开：请求 1 在途
    expect(holder.getReviewReport).toHaveBeenCalledTimes(1)
    expect(screen.getByText('报告全文加载中…')).toBeInTheDocument()

    fireEvent.click(preview) // 收起：cleanup 置 alive=false
    fireEvent.click(preview) // 再展开：请求 1 仍在途，fetchedRef 早退不发新请求
    expect(holder.getReviewReport).toHaveBeenCalledTimes(1)

    // 请求 1 完成但结果被丢弃：内部应重置防重入并重新发起请求（第二次走 beforeEach 默认实现，立即返回）
    const found = REPORTS.find((r) => r.id === 6)!
    resolveFirst({ ...found, reportMd: `${found.reportMd}\n\n完整版追加段落。` })
    await waitFor(() => expect(holder.getReviewReport).toHaveBeenCalledTimes(2))

    expect(await screen.findByText(/完整版追加段落。/)).toBeInTheDocument()
    expect(screen.queryByText('报告全文加载中…')).not.toBeInTheDocument()
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

  it('立即复盘点火成功：提示已启动、按钮立即恢复且不主动刷新列表，ignite 事件携带预分配 roundId', async () => {
    render(<ReviewPanel />)
    await screen.findByText(/第 5 份报告/)
    const callsBefore = holder.getReviewReports.mock.calls.length
    const igniteSpy = vi.fn()
    window.addEventListener('review-round-ignite', igniteSpy)

    fireEvent.click(screen.getByRole('button', { name: '立即复盘' }))

    expect(await screen.findByText('复盘已启动，进度见下方状态条')).toBeInTheDocument()
    expect(holder.runReview).toHaveBeenCalledTimes(1)
    // 点火事件 detail 携带 POST 预分配的审计轮 ID（状态条据此 pinned 绑定本轮）
    expect(igniteSpy).toHaveBeenCalledTimes(1)
    expect((igniteSpy.mock.calls[0][0] as CustomEvent).detail).toEqual({ roundId: 'rv-ignite' })
    window.removeEventListener('review-round-ignite', igniteSpy)
    // 点火即返回：按钮立即恢复；列表不随点火刷新（结果经状态条 onFinished 刷新）
    expect(screen.getByRole('button', { name: '立即复盘' })).toBeEnabled()
    expect(holder.getReviewReports).toHaveBeenCalledTimes(callsBefore)
  })

  it('立即复盘 409（进行中）：按成功样式提示 ApiError.detail，不用错误红', async () => {
    holder.runReview.mockRejectedValueOnce(new ApiError(409, '复盘进行中'))
    render(<ReviewPanel />)
    await screen.findByText(/第 5 份报告/)

    fireEvent.click(screen.getByRole('button', { name: '立即复盘' }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('复盘进行中')
    expect(alert.className).toContain('emerald')
    expect(alert.className).not.toContain('rose')
  })

  it('立即复盘 409 且状态条未激活：广播 catchup 事件让状态条经补漏找回进行中轮', async () => {
    holder.runReview.mockRejectedValueOnce(new ApiError(409, '复盘进行中'))
    render(<ReviewPanel />)
    await screen.findByText(/第 5 份报告/)
    expect(screen.queryByTestId('review-live-strip')).not.toBeInTheDocument()

    // 他处（别的标签页/自动调度）已点火：/live 可见进行中轮；本页 WS 断线收不到 start 事件
    holder.getReviewLive.mockResolvedValue({ round: LIVE_ROUND, tool_calls: [] })
    fireEvent.click(screen.getByRole('button', { name: '立即复盘' }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('复盘进行中')
    expect(alert.className).toContain('emerald')
    expect(await screen.findByTestId('review-live-strip')).toBeInTheDocument()
  })

  it('立即复盘 503：红字展示 ApiError.detail（LLM 未配置）', async () => {
    holder.runReview.mockRejectedValueOnce(new ApiError(503, 'LLM 未配置'))
    render(<ReviewPanel />)
    await screen.findByText(/第 5 份报告/)

    fireEvent.click(screen.getByRole('button', { name: '立即复盘' }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('LLM 未配置')
    expect(alert.className).toContain('rose')
  })

  it('点火联动：点火后状态条不经 WS 即激活，WS 轮结束事件后状态条消失、提示清空并自动刷新列表', async () => {
    const { rerender } = render(<ReviewPanel />)
    await screen.findByText(/第 5 份报告/)
    expect(screen.queryByTestId('review-live-strip')).not.toBeInTheDocument()

    // 点火：绿提示出现 + 状态条经 review-round-ignite 事件激活（覆盖 WS 断线窗口内点火场景）
    fireEvent.click(screen.getByRole('button', { name: '立即复盘' }))
    expect(await screen.findByText('复盘已启动，进度见下方状态条')).toBeInTheDocument()
    expect(await screen.findByTestId('review-live-strip')).toBeInTheDocument()

    // WS 注入复盘结束事件：状态条消失，onFinished（即 refreshToLatest）清提示并自动刷新报告列表
    const callsBefore = holder.getReviewReports.mock.calls.length
    holder.lastMessage = { type: 'review_round', data: { round_id: 'rv-ignite', ok: true } }
    rerender(<ReviewPanel />)

    await waitFor(() => expect(screen.queryByTestId('review-live-strip')).not.toBeInTheDocument())
    await waitFor(() => expect(holder.getReviewReports).toHaveBeenCalledTimes(callsBefore + 1))
    expect(screen.queryByText('复盘已启动，进度见下方状态条')).not.toBeInTheDocument()
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
