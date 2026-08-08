/**
 * 研报面板测试：列表渲染（时间/类型徽标/方向徽标/置信度徽标/error 红字）、展开详情
 * （结论条 + narrative 全文 + 证据/风险列表 + 因果链 chip 节点链，字段缺失整块降级）、
 * 「生成研报」成功刷新与 409/503 的 ApiError.detail 提示、服务端分页、工具调用链内嵌
 * （roundId 非空 lazy 拉取 getRound；空串 = 老研报灰字降级且不拉取）。
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../api/http'
import type { ResearchReportDetail, ResearchReportSummary, RoundDetail, WsMessage } from '../api/types'
import ResearchPanel from '../components/console/ResearchPanel'

const iso = (unixSec: number) => new Date(unixSec * 1000).toISOString()

/**
 * 7 条研报覆盖两页（最新在前）：id7 失败红字行（roundId 空串）；id6 亚盘成功（偏多/中/24h，
 * roundId 非空，演示工具链内嵌）；id5~1 手动普通行（中性/低，roundId 空串降级）。
 */
const REPORTS: ResearchReportSummary[] = [
  {
    id: 7,
    reportType: 'manual',
    direction: '中性',
    confidence: '低',
    horizon: '',
    evidenceJson: '[]',
    risksJson: '[]',
    narrative: '',
    rawJson: '{}',
    verifyResult: '',
    error: 'LLM 响应超时',
    roundId: '',
    time: iso(1784600000),
  },
  {
    id: 6,
    reportType: 'asia_open',
    direction: '偏多',
    confidence: '中',
    horizon: '24h',
    evidenceJson: '["美国 6 月 CPI 同比 3.0%，低于预期 3.1%"]',
    risksJson: '["美联储官员讲话偏鹰或压制风险偏好"]',
    narrative: '亚盘时段宏观面偏多：美国 6 月 CPI 同比回落至 3.0%，美元指数走弱，实际利率下行。',
    rawJson: '{"direction":"偏多"}',
    verifyResult: '',
    error: '',
    roundId: 'rs-round-6',
    time: iso(1784510000),
  },
  ...Array.from({ length: 5 }, (_, i) => {
    const id = 5 - i
    return {
      id,
      reportType: 'manual',
      direction: '中性',
      confidence: '低',
      horizon: '',
      evidenceJson: '[]',
      risksJson: '[]',
      narrative: `第 ${id} 份研报：方向中性，等待更清晰的事件驱动。`,
      rawJson: '{}',
      verifyResult: '',
      error: '',
      roundId: '',
      time: iso(1784420000 - i * 3600),
    }
  }),
]

/** id6 关联的研报审计轮详情：2 条工具调用 + Anthropic 原生格式 llm_raw */
const ROUND_DETAIL: RoundDetail = {
  round_id: 'rs-round-6',
  prompt_snapshot: 'prompt 快照',
  llm_raw: JSON.stringify({
    role: 'assistant',
    content: [
      { type: 'text', text: '先拉取宏观上下文，再核对消息面。' },
      { type: 'tool_use', id: 'toolu_rs_1', name: 'get_macro_context', input: { hours: 24 } },
    ],
  }),
  tool_calls: [
    {
      seq: 1,
      tool: 'get_macro_context',
      args: { hours: 24 },
      risk_verdict: '',
      risk_reason: '',
      result: '{"cpi":"3.0%"}',
      duration_ms: 8,
    },
    {
      seq: 2,
      tool: 'get_news_flash',
      args: { keyword: 'ETF' },
      risk_verdict: '',
      risk_reason: '',
      result: '{"count":3}',
      duration_ms: 5,
    },
  ],
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

// ResearchLiveStrip 经 useWs 订阅研报事件；lastMessage 经 holder 可控派发（默认 null 无消息，进度条隐藏）
vi.mock('../hooks/useWs', () => ({
  useWs: () => ({ connected: true, lastMessage: holder.lastMessage }),
}))

beforeEach(() => {
  vi.clearAllMocks()
  holder.lastMessage = null
  // 默认无进行中研报轮：进度条不渲染
  holder.getResearchLive.mockResolvedValue({ round: null, tool_calls: [] })
  holder.getResearchReports.mockImplementation((offset: number, limit: number) =>
    Promise.resolve({ items: REPORTS.slice(offset, offset + limit), total: REPORTS.length }),
  )
  // 详情在列表基础上追加「完整版追加段落」与证据/风险/因果链，验证展开时 lazy 拉取的是全文而非列表截断
  holder.getResearchReport.mockImplementation((id: number): Promise<ResearchReportDetail> => {
    const found = REPORTS.find((r) => r.id === id)
    if (!found) return Promise.reject(new ApiError(404, `研报不存在: ${id}`))
    if (id !== 6) {
      return Promise.resolve({
        ...found,
        narrative: found.narrative === '' ? '' : `${found.narrative}\n\n完整版追加段落。`,
        evidence: [],
        risks: [],
        raw: {},
        causalLinks: [],
      })
    }
    return Promise.resolve({
      ...found,
      narrative: `${found.narrative}\n\n完整版追加段落。`,
      evidence: ['美国 6 月 CPI 同比 3.0%，低于预期 3.1%', 'BTC 现货 ETF 连续三日净流入'],
      risks: ['美联储官员讲话偏鹰或压制风险偏好', '亚盘流动性偏薄，波动易被放大'],
      raw: { direction: '偏多', confidence: '中', horizon: '24h' },
      causalLinks: [
        {
          id: 1,
          reportId: 6,
          chain: [
            { node: '美国 6 月 CPI 同比回落至 3.0%', kind: '事件', timeline_id: 1287 },
            { node: '美元指数走弱、实际利率下行', kind: '市场反应' },
            { node: '风险资产偏好修复，BTC 获增量资金流入', kind: '标的结论' },
          ],
          confidence: 0.72,
          evidence: ['金十日历：CPI 公布值 3.0% 低于预期 3.1%', '律动快讯：BTC ETF 单日净流入 2.1 亿美元'],
          status: 'verified',
          brokenAt: null,
          topic: 'CPI',
          supersedesId: null,
          awaitVerification: false,
          time: iso(1784510000),
        },
        {
          id: 3,
          reportId: 6,
          chain: [
            { node: '美联储官员鹰派讲话', kind: '事件' },
            { node: '降息预期降温', kind: '推断' },
          ],
          confidence: 0.5,
          evidence: ['金十快讯：鲍威尔称「不急于降息」'],
          status: 'superseded',
          brokenAt: null,
          topic: '美联储',
          supersedesId: null,
          awaitVerification: false, // 旧版结论链被替代：不得再显示「结论」徽标
          time: iso(1784510000),
        },
        {
          id: 2,
          reportId: 6,
          chain: [
            { node: '美联储官员鹰派讲话', kind: '事件' },
            { node: '降息预期降温', kind: '推断' },
            { node: '加密市场短线承压回落', kind: '标的结论' },
          ],
          confidence: 0.55,
          evidence: ['金十快讯：鲍威尔称「不急于降息」'],
          status: 'pending',
          brokenAt: null,
          topic: '美联储',
          supersedesId: 3, // 修正版：替代链#3
          awaitVerification: true,
          time: iso(1784510000),
        },
        {
          id: 4,
          reportId: 6,
          chain: [{ node: '旧数据无主题的链', kind: '事件' }],
          confidence: 0.4,
          evidence: [],
          status: 'pending',
          brokenAt: null,
          topic: '', // 旧数据形态 → 「未分组」族
          supersedesId: null,
          awaitVerification: true,
          time: iso(1784510000),
        },
      ],
    })
  })
  holder.runResearch.mockImplementation(() =>
    Promise.resolve({
      started: true,
      ok: true,
      reportId: 8,
      roundId: 'rs-8',
      direction: '中性',
      confidence: '低',
      error: '',
    }),
  )
  // 默认给任意 roundId 返回同一份审计详情（id6 展开即触发）
  holder.getRound.mockResolvedValue(ROUND_DETAIL)
})

describe('ResearchPanel(研报面板)', () => {
  it('列表渲染：error 红字行 / 类型徽标 / 方向徽标 / 置信度徽标 / 分页摘要', async () => {
    render(<ResearchPanel />)

    expect(await screen.findByText('研报失败：LLM 响应超时')).toBeInTheDocument()
    expect(screen.getByText('亚盘')).toBeInTheDocument()
    expect(screen.getAllByText('手动').length).toBe(4) // id7/5/4/3 四条 manual（失败行同样带类型徽标）
    expect(screen.getByText('偏多')).toBeInTheDocument()
    expect(screen.getAllByText('中性').length).toBe(4)
    expect(screen.getByText('置信度 中')).toBeInTheDocument()
    expect(screen.getAllByText('置信度 低').length).toBe(4)
    expect(screen.getByText('第 1/2 页 · 共 7 条研报')).toBeInTheDocument()
    expect(holder.getResearchReports).toHaveBeenCalledWith(0, 5)
  })

  it('点击展开：lazy 拉取全文，展示结论条、narrative 全文、证据/风险列表与因果链 chip 节点链', async () => {
    render(<ResearchPanel />)
    const preview = await screen.findByText(/亚盘时段宏观面偏多/)

    fireEvent.click(preview)
    await waitFor(() => expect(holder.getResearchReport).toHaveBeenCalledWith(6))

    // 结论条：方向 + 置信度 + 前瞻窗口（horizon 非空才渲染）
    // 「偏多」在摘要行徽标与详情结论条各出现一次（展开后为 2 处），断言 >=2 而非精确计数
    expect(await screen.findByText('前瞻窗口 24h')).toBeInTheDocument()
    expect(screen.getAllByText('偏多').length).toBeGreaterThanOrEqual(2)
    // 全文（列表预览不含追加段落）
    expect(await screen.findByText(/完整版追加段落。/)).toBeInTheDocument()
    // 证据/风险列表
    expect(screen.getByText('证据')).toBeInTheDocument()
    expect(screen.getByText('· 美国 6 月 CPI 同比 3.0%，低于预期 3.1%')).toBeInTheDocument()
    expect(screen.getByText('· BTC 现货 ETF 连续三日净流入')).toBeInTheDocument()
    expect(screen.getByText('风险')).toBeInTheDocument()
    expect(screen.getByText('· 美联储官员讲话偏鹰或压制风险偏好')).toBeInTheDocument()
    // 因果链：按主题分族标题 + 状态徽标 + 链置信度 + chip 节点链（→ 串联）+ timeline 溯源标注
    expect(screen.getByText('因果链（按主题分族）')).toBeInTheDocument()
    expect(screen.getByText('已确认')).toBeInTheDocument()
    expect(screen.getAllByText('待验证').length).toBe(2) // id2 修正版 + id4 未分组旧链
    expect(screen.getByText('已被替代')).toBeInTheDocument() // superseded 状态徽标
    expect(screen.getByText('结论')).toBeInTheDocument() // awaitVerification=false 且未被替代的结论徽标
    expect(screen.getByText('替代链#3')).toBeInTheDocument() // 修正版标注替代目标
    expect(screen.getByText('已被链#2替代')).toBeInTheDocument() // findReplacer 反查
    expect(screen.getByText('CPI')).toBeInTheDocument() // 分族标题
    expect(screen.getByText('美联储')).toBeInTheDocument()
    expect(screen.getByText('未分组')).toBeInTheDocument() // topic 空串降级
    expect(screen.getByText('链置信度 72%')).toBeInTheDocument()
    expect(screen.getByText('链置信度 55%')).toBeInTheDocument()
    expect(screen.getByText('美国 6 月 CPI 同比回落至 3.0%')).toBeInTheDocument()
    expect(screen.getByText('美元指数走弱、实际利率下行')).toBeInTheDocument()
    expect(screen.getByText('加密市场短线承压回落')).toBeInTheDocument()
    expect(screen.getByText('溯源 #1287')).toBeInTheDocument()
    expect(screen.getAllByText('→').length).toBeGreaterThanOrEqual(4) // 两条 3 节点链各 2 个箭头
    // 族内排序：美联储族当前版（id2 待验证）在前、历史版（id3 已被替代）在后
    // （按「链置信度」过滤出卡片 li，排除卡片内证据列表的嵌套 li）
    const fedGroup = screen.getByText('美联储').closest('div')!
    const fedCards = Array.from(fedGroup.querySelectorAll('li')).filter((li) =>
      li.textContent?.includes('链置信度'),
    )
    expect(fedCards[0].textContent).toContain('待验证')
    expect(fedCards[1].textContent).toContain('已被替代')
    // 被替代的结论链不再显示「结论」徽标（只有 CPI 族的 id1 有）
    expect(screen.getAllByText('结论').length).toBe(1)
  })

  it('慢请求回归：展开→请求未返回时收起→再展开→请求完成，最终显示详情而非永远加载中', async () => {
    // 第一次请求挂起（模拟慢请求），完成时机由测试可控
    let resolveFirst: (d: ResearchReportDetail) => void = () => {}
    holder.getResearchReport.mockImplementationOnce(
      () => new Promise<ResearchReportDetail>((resolve) => { resolveFirst = resolve }),
    )
    render(<ResearchPanel />)
    const preview = await screen.findByText(/亚盘时段宏观面偏多/)

    fireEvent.click(preview) // 展开：请求 1 在途
    expect(holder.getResearchReport).toHaveBeenCalledTimes(1)
    expect(screen.getByText('研报全文加载中…')).toBeInTheDocument()

    fireEvent.click(preview) // 收起：cleanup 置 alive=false
    fireEvent.click(preview) // 再展开：请求 1 仍在途，fetchedRef 早退不发新请求
    expect(holder.getResearchReport).toHaveBeenCalledTimes(1)

    // 请求 1 完成但结果被丢弃：内部应重置防重入并重新发起请求（第二次走 beforeEach 默认实现，立即返回）
    const found = REPORTS.find((r) => r.id === 6)!
    resolveFirst({
      ...found,
      narrative: `${found.narrative}\n\n完整版追加段落。`,
      evidence: [],
      risks: [],
      raw: {},
      causalLinks: [],
    })
    await waitFor(() => expect(holder.getResearchReport).toHaveBeenCalledTimes(2))

    expect(await screen.findByText(/完整版追加段落。/)).toBeInTheDocument()
    expect(screen.queryByText('研报全文加载中…')).not.toBeInTheDocument()
  })

  it('失败研报展开：error 行无证据/风险/因果链整块，roundId 空串灰字降级且不拉取 getRound', async () => {
    render(<ResearchPanel />)
    const errorRow = await screen.findByText('研报失败：LLM 响应超时')

    fireEvent.click(errorRow)
    await waitFor(() => expect(holder.getResearchReport).toHaveBeenCalledWith(7))
    await waitFor(() => expect(screen.queryByText('研报全文加载中…')).not.toBeInTheDocument())

    // 无数据整块不渲染（失败研报无证据/风险/因果链）
    expect(screen.queryByText('证据')).not.toBeInTheDocument()
    expect(screen.queryByText('风险')).not.toBeInTheDocument()
    expect(screen.queryByText('因果链（按主题分族）')).not.toBeInTheDocument()
    // 空串 roundId：灰字降级提示，且不拉取审计轮
    expect(screen.getByText('该研报无工具调用记录')).toBeInTheDocument()
    expect(holder.getRound).not.toHaveBeenCalled()
  })

  it('工具链内嵌：roundId 非空的研报展开后 lazy 拉取 getRound 并展示工具调用详情', async () => {
    render(<ResearchPanel />)
    const preview = await screen.findByText(/亚盘时段宏观面偏多/)

    fireEvent.click(preview)
    await waitFor(() => expect(holder.getRound).toHaveBeenCalledWith('rs-round-6'))

    expect(await screen.findByText('工具调用详情 · tool_calls（2 步）')).toBeInTheDocument()

    // 默认收起（设计规格）：工具步骤默认不在文档中，点击标题展开后可见
    // （锚点用 seq N 而非工具名：ConversationThread 的 details 内容始终在 DOM，会命中工具名）
    expect(screen.queryByText('seq 1')).not.toBeInTheDocument()
    fireEvent.click(screen.getByText('工具调用详情 · tool_calls（2 步）'))
    expect(await screen.findByText('seq 1')).toBeInTheDocument()
  })

  it('工具链 lazy：不展开任何研报时不拉取 getRound', async () => {
    render(<ResearchPanel />)
    await screen.findByText(/第 5 份研报/)

    expect(holder.getRound).not.toHaveBeenCalled()
  })

  it('生成研报成功：提示成功、以 manual/24h 触发并刷新列表', async () => {
    render(<ResearchPanel />)
    await screen.findByText(/第 5 份研报/)
    const callsBefore = holder.getResearchReports.mock.calls.length

    fireEvent.click(screen.getByRole('button', { name: '生成研报' }))

    expect(await screen.findByText('研报已生成，最新研报已入列')).toBeInTheDocument()
    expect(holder.runResearch).toHaveBeenCalledWith('manual', 24)
    await waitFor(() => expect(holder.getResearchReports).toHaveBeenCalledTimes(callsBefore + 1))
  })

  it('生成研报 409：展示 ApiError.detail（研报生成中）', async () => {
    holder.runResearch.mockRejectedValueOnce(new ApiError(409, '研报生成中'))
    render(<ResearchPanel />)
    await screen.findByText(/第 5 份研报/)

    fireEvent.click(screen.getByRole('button', { name: '生成研报' }))

    expect(await screen.findByText('研报生成中')).toBeInTheDocument()
  })

  it('生成研报 503：展示 ApiError.detail（LLM 未配置）', async () => {
    holder.runResearch.mockRejectedValueOnce(new ApiError(503, 'LLM 未配置'))
    render(<ResearchPanel />)
    await screen.findByText(/第 5 份研报/)

    fireEvent.click(screen.getByRole('button', { name: '生成研报' }))

    expect(await screen.findByText('LLM 未配置')).toBeInTheDocument()
  })

  it('生成研报返回 started=true 但 ok=false（研报执行失败）：红色提示失败原因且仍刷新列表', async () => {
    // 后端路由仅把「LLM 未配置」「生成中」「hours 越界」映 503/409/422，其余失败以 200 返回 ok=false（失败研报已落库）
    holder.runResearch.mockResolvedValueOnce({
      started: true,
      ok: false,
      reportId: 8,
      roundId: 'rs-8',
      direction: '中性',
      confidence: '低',
      error: 'LLM 调用异常',
    })
    render(<ResearchPanel />)
    await screen.findByText(/第 5 份研报/)
    const callsBefore = holder.getResearchReports.mock.calls.length

    fireEvent.click(screen.getByRole('button', { name: '生成研报' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('研报失败：LLM 调用异常')
    await waitFor(() => expect(holder.getResearchReports).toHaveBeenCalledTimes(callsBefore + 1))
  })

  it('分页：下一页拉取 offset=5 并渲染第二页内容', async () => {
    render(<ResearchPanel />)
    await screen.findByText(/第 5 份研报/)

    fireEvent.click(screen.getByRole('button', { name: '下一页' }))

    await waitFor(() => expect(holder.getResearchReports).toHaveBeenLastCalledWith(5, 5))
    expect(await screen.findByText('第 2 份研报：方向中性，等待更清晰的事件驱动。')).toBeInTheDocument()
    expect(screen.getByText('第 1 份研报：方向中性，等待更清晰的事件驱动。')).toBeInTheDocument()
    expect(screen.getByText('第 2/2 页 · 共 7 条研报')).toBeInTheDocument()
  })

  it('进度条联动：research_round_start 出现进度条，research_round 结束后消失并自动刷新研报列表', async () => {
    const { rerender } = render(<ResearchPanel />)
    await screen.findByText(/第 5 份研报/)
    expect(screen.queryByTestId('research-live-strip')).not.toBeInTheDocument()

    // 注入研报开始事件：进度条出现（轮询数据源返回进行中轮与 1 条 {text} 包装的工具调用）
    holder.getResearchLive.mockResolvedValue({
      round: {
        round_id: 'rs-live',
        wake_source: 'research',
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
          tool: 'get_macro_context',
          args: { hours: 24 },
          risk_verdict: '',
          risk_reason: '',
          result: { text: '概览' },
          duration_ms: 12,
        },
      ],
    })
    holder.lastMessage = { type: 'research_round_start', data: { round_id: 'rs-live' } }
    rerender(<ResearchPanel />)
    expect(await screen.findByTestId('research-live-strip')).toBeInTheDocument()

    // 注入研报结束事件：进度条消失，onFinished（即 refreshToLatest）触发研报列表自动刷新
    const callsBefore = holder.getResearchReports.mock.calls.length
    holder.lastMessage = { type: 'research_round', data: { round_id: 'rs-live', ok: true } }
    rerender(<ResearchPanel />)

    await waitFor(() => expect(screen.queryByTestId('research-live-strip')).not.toBeInTheDocument())
    await waitFor(() => expect(holder.getResearchReports).toHaveBeenCalledTimes(callsBefore + 1))
  })
})
