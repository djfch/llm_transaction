/**
 * 决策时间线（console）测试：分页渲染与「加载更多」追加、卡片展开 lazy 拉取 getRound、
 * RoundFocus 定位已加载轮 → 展开 + jump-hl 高亮类 + scrollIntoView；
 * 定位未加载轮 → 逐页加载；全部耗尽仍无 → 顶部提示「未找到该决策轮」；
 * WS round 事件 → 失效信号重拉第一页去重前合（payload 不作数据源）。
 * ApiClient 全量 mock；WS 经 wsHolder.lastMessage 可控派发（jsdom 无 WebSocket）。
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Note, RoundDetail, RoundSummary, WsMessage } from '../api/types'
import RoundTimeline from '../components/console/RoundTimeline'
import { RoundFocusProvider, useRoundFocus } from '../hooks/useRoundFocus'

// 25 轮（第一页 20 + 第二页 5），验证分页追加与「无更多」
const ROUNDS: RoundSummary[] = Array.from({ length: 25 }, (_, i) => ({
  round_id: `id-${100 - i}`,
  started_at: new Date(1_700_000_000_000 - i * 3_600_000).toISOString(),
  wake_source: ['定时唤醒', '价格触发', '手动唤醒'][i % 3],
  summary: `第 ${100 - i} 轮结论摘要`,
}))

const holder = vi.hoisted(() => ({
  getRounds: vi.fn() as ReturnType<typeof vi.fn<(o: number, l: number) => Promise<RoundSummary[]>>>,
  getRound: vi.fn() as ReturnType<typeof vi.fn<(id: string) => Promise<RoundDetail>>>,
  getNotes: vi.fn() as ReturnType<typeof vi.fn<() => Promise<Note[]>>>,
}))
vi.mock('../api', () => ({
  api: {
    getRounds: (o: number, l: number) => holder.getRounds(o, l),
    getRound: (id: string) => holder.getRound(id),
    getNotes: () => holder.getNotes(),
  },
}))
// WS 可控桩：测试改写 wsHolder.lastMessage 后 rerender 即可派发消息
const wsHolder = vi.hoisted(() => ({ lastMessage: null as WsMessage | null }))
vi.mock('../hooks/useWs', () => ({
  useWs: () => ({ connected: true, lastMessage: wsHolder.lastMessage }),
}))

beforeAll(() => {
  // jsdom 未实现 scrollIntoView，注入桩
  window.HTMLElement.prototype.scrollIntoView = vi.fn()
})

/** 审计详情夹具：2 次工具调用（含一次 deny）+ llm_raw 原文标记 */
function detail(id: string): RoundDetail {
  return {
    round_id: id,
    prompt_snapshot: 'prompt',
    llm_raw: `RAW-${id}`,
    tool_calls: [
      { seq: 1, tool: 'get_account', args: {}, risk_verdict: '', risk_reason: '', result: 'ok', duration_ms: 5 },
      {
        seq: 2,
        tool: 'place_order',
        args: { size: 1 },
        risk_verdict: 'deny',
        risk_reason: '超限',
        result: '风控拒绝，未下单',
        duration_ms: 8,
      },
    ],
  }
}

beforeEach(() => {
  wsHolder.lastMessage = null
  holder.getRounds = vi
    .fn<(o: number, l: number) => Promise<RoundSummary[]>>()
    .mockImplementation((offset, limit) => Promise.resolve(ROUNDS.slice(offset, offset + limit)))
  holder.getRound = vi
    .fn<(id: string) => Promise<RoundDetail>>()
    .mockImplementation((id) => Promise.resolve(detail(id)))
  holder.getNotes = vi.fn<() => Promise<Note[]>>().mockResolvedValue([])
})

/** focus 触发器：按钮点击即定位指定轮 */
function FocusTrigger({ roundId }: { roundId: string }) {
  const { focus } = useRoundFocus()
  return (
    <button data-testid="focus-btn" onClick={() => focus(roundId)}>
      focus
    </button>
  )
}

/** 时间线 JSX（rerender 派发 WS 消息时需同一结构） */
function timelineUi(focusId: string) {
  return (
    <RoundFocusProvider>
      <FocusTrigger roundId={focusId} />
      <RoundTimeline />
    </RoundFocusProvider>
  )
}

function renderTimeline(focusId = '') {
  return render(timelineUi(focusId))
}

describe('RoundTimeline(决策时间线)', () => {
  it('首屏渲染第一页卡片（短号/唤醒徽标/summary）；「加载更多」追加第二页后消失', async () => {
    renderTimeline()

    expect(await screen.findByText('第 100 轮结论摘要')).toBeInTheDocument()
    expect(screen.getByText('第 81 轮结论摘要')).toBeInTheDocument() // 第一页末条
    expect(screen.queryByText('第 80 轮结论摘要')).not.toBeInTheDocument()
    // 短号（含分隔符取末段）与唤醒来源徽标
    expect(screen.getByText('#100')).toBeInTheDocument()
    expect(screen.getAllByText('定时唤醒').length).toBeGreaterThan(0)
    expect(screen.getAllByText('价格触发').length).toBeGreaterThan(0)
    expect(screen.getAllByText('手动唤醒').length).toBeGreaterThan(0)

    fireEvent.click(screen.getByRole('button', { name: '加载更多' }))
    expect(await screen.findByText('第 76 轮结论摘要')).toBeInTheDocument() // 第二页末条
    expect(holder.getRounds).toHaveBeenLastCalledWith(20, 20)
    // 第二页不足 PAGE_SIZE → 无更多
    expect(screen.queryByRole('button', { name: '加载更多' })).not.toBeInTheDocument()
  })

  it('WS round 事件：payload 不作数据源，重拉第一页去重前合（新轮完整数据置顶）', async () => {
    const NEW: RoundSummary = {
      round_id: 'id-101',
      started_at: new Date(1_700_000_000_000 + 3_600_000).toISOString(),
      wake_source: '价格触发',
      summary: '第 101 轮结论摘要',
    }
    const { rerender } = renderTimeline()
    await screen.findByText('第 100 轮结论摘要')
    holder.getRounds.mockClear()
    // 服务端落库新轮后广播 round（payload 仅 {round_id, ok, wake_source}，无摘要/时间）
    holder.getRounds.mockImplementation((offset, limit) =>
      Promise.resolve([NEW, ...ROUNDS].slice(offset, offset + limit)),
    )
    wsHolder.lastMessage = {
      type: 'round',
      data: { round_id: 'id-101', ok: true, wake_source: '价格触发' },
    }
    rerender(timelineUi(''))

    // 新轮以 REST 完整数据置顶（若直接消费 payload，摘要/时间将空白）
    expect(await screen.findByText('第 101 轮结论摘要')).toBeInTheDocument()
    expect(holder.getRounds).toHaveBeenCalledWith(0, 20)
    // 已有卡片不丢（去重前合）
    expect(screen.getByText('第 100 轮结论摘要')).toBeInTheDocument()
  })

  it('WS round 事件：同步重拉笔记映射（回归 M2/L6：新轮可能带来新归属笔记）', async () => {
    const { rerender } = renderTimeline()
    await screen.findByText('第 100 轮结论摘要')
    expect(holder.getNotes).toHaveBeenCalledTimes(1) // 挂载时一次

    wsHolder.lastMessage = {
      type: 'round',
      data: { round_id: 'id-101', ok: true, wake_source: '价格触发' },
    }
    rerender(timelineUi(''))

    await waitFor(() => expect(holder.getNotes).toHaveBeenCalledTimes(2))
  })

  it('点击卡片展开 → lazy 拉取 getRound，渲染 ToolSteps 工具链与完整对话折叠区', async () => {
    renderTimeline()
    const summary = await screen.findByText('第 100 轮结论摘要')
    expect(holder.getRound).not.toHaveBeenCalled()

    fireEvent.click(summary)
    await waitFor(() => expect(holder.getRound).toHaveBeenCalledWith('id-100'))
    // 工具调用详情（默认展开，ToolSteps）：工具名 + deny 风控理由行内展示
    expect(await screen.findByText('place_order')).toBeInTheDocument()
    expect(screen.getAllByText(/risk_reason\(风控理由\)：超限/).length).toBeGreaterThan(0)
    // 完整对话（ConversationThread 自带 details）：默认收起，点击摘要展开后可见 llm_raw 原文
    const chatSummary = screen.getByText(/完整对话 · agent loop/)
    const chatDetails = chatSummary.closest('details')!
    expect(chatDetails).not.toHaveAttribute('open')
    fireEvent.click(chatSummary)
    expect(chatDetails).toHaveAttribute('open')
    expect(screen.getByText('RAW-id-100')).toBeInTheDocument()
  })

  it('归属笔记的卡片渲染引文块（紫色左边条 + 斜体 + Agent 笔记 · HH:MM），无归属不渲染', async () => {
    holder.getNotes.mockResolvedValue([
      { time: '2023-11-14T22:20:00.000Z', content: '趋势里分批止盈比一次清仓更稳。', round_id: 'id-100' },
      { time: '2023-11-14T22:30:00.000Z', content: '无归属笔记', round_id: '' },
    ])
    renderTimeline()
    await screen.findByText('第 100 轮结论摘要')

    // 引文出现在归属轮卡片内：斜体内容 + 署名前缀
    const card = document.querySelector('[data-round-id="id-100"]')!
    const quote = await screen.findByText(/趋势里分批止盈比一次清仓更稳。/)
    expect(card.contains(quote)).toBe(true)
    expect(quote.tagName).toBe('BLOCKQUOTE')
    expect(quote.className).toContain('italic')
    expect(quote.className).toContain('border-violet-400/50')
    expect(quote.textContent).toContain('—— Agent 笔记 ·')
    // 空 round_id 笔记不入映射；其他卡片无引文
    expect(screen.queryByText(/无归属笔记/)).not.toBeInTheDocument()
    expect(document.querySelector('[data-round-id="id-99"] blockquote')).toBeNull()
  })

  it('同轮多条归属笔记：取最新一条（回归：依赖 http 适配层降序契约，首条即最新）', async () => {
    // getNotes 契约=最新在前（http 适配层保证），notesMap 首见即最新
    holder.getNotes.mockResolvedValue([
      { time: '2023-11-14T23:00:00.000Z', content: '最新结论：突破确认再加仓。', round_id: 'id-100' },
      { time: '2023-11-14T22:00:00.000Z', content: '较早记录：先观察量能。', round_id: 'id-100' },
    ])
    renderTimeline()
    await screen.findByText('第 100 轮结论摘要')

    expect(await screen.findByText(/最新结论：突破确认再加仓。/)).toBeInTheDocument()
    expect(screen.queryByText(/较早记录：先观察量能。/)).not.toBeInTheDocument()
  })

  it('每张卡片都有灰色「已完成」徽标（历史轮静态文案）', async () => {
    renderTimeline()
    await screen.findByText('第 100 轮结论摘要')
    // 第一页 20 张卡片各一枚
    expect(screen.getAllByText('已完成')).toHaveLength(20)
  })

  it('focus 已加载轮 → 展开（拉详情）+ jump-hl 高亮类 + scrollIntoView', async () => {
    renderTimeline('id-98')
    await screen.findByText('第 98 轮结论摘要')

    fireEvent.click(screen.getByTestId('focus-btn'))
    // 展开该卡（触发 lazy getRound）
    await waitFor(() => expect(holder.getRound).toHaveBeenCalledWith('id-98'))
    // 高亮类 + 平滑滚动到卡片
    const card = document.querySelector('[data-round-id="id-98"]')
    expect(card?.classList.contains('jump-hl')).toBe(true)
    await waitFor(() => expect(window.HTMLElement.prototype.scrollIntoView).toHaveBeenCalled())
  })

  it('focus 未加载轮 → 逐页加载；全部耗尽仍无 → 顶部提示「未找到该决策轮」', async () => {
    renderTimeline('ghost-round')
    await screen.findByText('第 100 轮结论摘要')
    holder.getRounds.mockClear()

    fireEvent.click(screen.getByTestId('focus-btn'))
    expect(await screen.findByText(/未找到该决策轮：ghost-round/)).toBeInTheDocument()
    // 25 轮：offset 20 追加到第二页（5 条，不足一页即判定耗尽，共 1 次追加请求）
    expect(holder.getRounds).toHaveBeenCalledTimes(1)
    expect(holder.getRounds).toHaveBeenNthCalledWith(1, 20, 20)
  })
})
