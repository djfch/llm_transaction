/**
 * 决策时间线测试：服务端分页、最多五个页码、跳页、WS 刷新回退、卡片详情与跨页定位。
 * API 与 WebSocket 均使用可控桩，确保测试验证页面状态而非网络实现。
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import type {
  Note,
  NotesPageResult,
  RoundDetail,
  RoundsPageResult,
  RoundSummary,
  StrategyVersion,
  WsMessage,
} from '../api/types'
import RoundTimeline from '../components/console/RoundTimeline'
import { RoundFocusProvider, useRoundFocus } from '../hooks/useRoundFocus'

/**
 * 37 轮决策足以覆盖 8 页数据与五页码滑动窗口。
 * strategyMd5 分布覆盖版本标签三种形态：匹配版本表（md5-aaa/bbb）/ 无匹配（md5-unknown）/ 空串。
 */
const ROUNDS: RoundSummary[] = Array.from({ length: 37 }, (_, index) => ({
  round_id: `id-${100 - index}`,
  started_at: new Date(1_700_000_000_000 - index * 3_600_000).toISOString(),
  wake_source: ['定时唤醒', '价格触发', '手动唤醒'][index % 3],
  summary: `第 ${100 - index} 轮结论摘要`,
  strategyMd5: index % 5 === 4 ? '' : index % 5 === 3 ? 'md5-unknown' : ['md5-aaa', 'md5-bbb'][index % 2],
}))

/** 策略版本表夹具（最新在前）：md5-aaa→v2 复盘、md5-bbb→v3 人工 */
const VERSIONS: StrategyVersion[] = [
  { id: 3, md5: 'md5-bbb', createdBy: 'human', reason: '', reportId: null, time: '2026-07-26T00:00:00.000Z' },
  { id: 2, md5: 'md5-aaa', createdBy: 'review_agent', reason: '复盘改写', reportId: 1, time: '2026-07-25T00:00:00.000Z' },
]

/** 将任意列表切片为与后端一致的分页响应。 */
function pageOf<T>(items: T[], offset: number, limit: number) {
  return { items: items.slice(offset, offset + limit), total: items.length, offset, limit }
}

/** 建立可由测试主动完成的 Promise，用于验证首屏请求尚未结束时的交互。 */
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((finish) => {
    resolve = finish
  })
  return { promise, resolve }
}

const holder = vi.hoisted(() => ({
  getRounds: vi.fn(),
  getRound: vi.fn(),
  getNotes: vi.fn(),
  getStrategyVersions: vi.fn(),
}))
vi.mock('../api', () => ({
  api: {
    getRounds: (offset: number, limit: number) => holder.getRounds(offset, limit),
    getRound: (roundId: string) => holder.getRound(roundId),
    getNotes: (offset?: number, limit?: number) => holder.getNotes(offset, limit),
    getStrategyVersions: () => holder.getStrategyVersions(),
  },
}))

/** 测试通过重渲染派发可控的 WebSocket 消息。 */
const wsHolder = vi.hoisted(() => ({ lastMessage: null as WsMessage | null }))
vi.mock('../hooks/useWs', () => ({
  useWs: () => ({ connected: true, lastMessage: wsHolder.lastMessage }),
}))

/** 构造可展开的审计详情夹具。 */
function detail(roundId: string): RoundDetail {
  return {
    round_id: roundId,
    prompt_snapshot: 'prompt',
    llm_raw: `RAW-${roundId}`,
    tool_calls: [
      { seq: 1, tool: 'get_account', args: {}, risk_verdict: '', risk_reason: '', result: 'ok', duration_ms: 5 },
    ],
    strategyMd5: 'md5-aaa',
  }
}

let currentRounds: RoundSummary[]
let currentNotes: Note[]

beforeAll(() => {
  window.HTMLElement.prototype.scrollIntoView = vi.fn()
})

beforeEach(() => {
  currentRounds = [...ROUNDS]
  currentNotes = []
  wsHolder.lastMessage = null
  vi.clearAllMocks()
  holder.getRounds.mockImplementation((offset: number, limit: number): Promise<RoundsPageResult> =>
    Promise.resolve(pageOf(currentRounds, offset, limit)),
  )
  holder.getRound.mockImplementation((roundId: string): Promise<RoundDetail> => Promise.resolve(detail(roundId)))
  holder.getNotes.mockImplementation((offset = 0, limit = 20): Promise<NotesPageResult> =>
    Promise.resolve(pageOf(currentNotes, offset, limit)),
  )
  holder.getStrategyVersions.mockImplementation((): Promise<StrategyVersion[]> => Promise.resolve([...VERSIONS]))
})

/** 焦点按钮模拟成交表或 K 线向时间线发送的 round_id 定位请求。 */
function FocusTrigger({ roundId }: { roundId: string }) {
  const { focus } = useRoundFocus()
  return (
    <button data-testid="focus-btn" onClick={() => focus(roundId)}>
      focus
    </button>
  )
}

/** 渲染包含焦点上下文的完整时间线。 */
function timelineUi(focusId: string) {
  return (
    <RoundFocusProvider>
      <FocusTrigger roundId={focusId} />
      <RoundTimeline />
    </RoundFocusProvider>
  )
}

/** 渲染时间线并允许测试修改焦点目标。 */
function renderTimeline(focusId = '') {
  return render(timelineUi(focusId))
}

describe('RoundTimeline(决策时间线)', () => {
  it('每页渲染 5 条、显示总数，并以最多五个页码切换下一页', async () => {
    renderTimeline()

    expect(await screen.findByText('第 100 轮结论摘要')).toBeInTheDocument()
    expect(screen.getByText('第 96 轮结论摘要')).toBeInTheDocument()
    expect(screen.queryByText('第 95 轮结论摘要')).not.toBeInTheDocument()
    expect(screen.getByText('共 37 条决策')).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: /第 \d+ 页/ })).toHaveLength(5)

    fireEvent.click(screen.getByRole('button', { name: '下一页' }))
    expect(await screen.findByText('第 95 轮结论摘要')).toBeInTheDocument()
    expect(holder.getRounds).toHaveBeenLastCalledWith(5, 5)
  })

  it('跳转到第 6 页后页码窗口随当前页滑动，非法页码不请求接口', async () => {
    renderTimeline()
    await screen.findByText('第 100 轮结论摘要')
    const input = screen.getByLabelText('跳转到第几页决策')

    fireEvent.change(input, { target: { value: '6' } })
    fireEvent.click(screen.getByRole('button', { name: '跳转' }))
    expect(await screen.findByText('第 75 轮结论摘要')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '第 1 页' })).not.toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: /第 \d+ 页/ })).toHaveLength(5)

    const callsBefore = holder.getRounds.mock.calls.length
    fireEvent.change(screen.getByLabelText('跳转到第几页决策'), { target: { value: '9' } })
    fireEvent.click(screen.getByRole('button', { name: '跳转' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('请输入 1 至 8 的整数页码')
    expect(holder.getRounds).toHaveBeenCalledTimes(callsBefore)
  })

  it('WS round 事件重拉当前页和总数，页码越界时自动回到最后有效页', async () => {
    const { rerender } = renderTimeline()
    await screen.findByText('第 100 轮结论摘要')
    const input = screen.getByLabelText('跳转到第几页决策')
    fireEvent.change(input, { target: { value: '8' } })
    fireEvent.click(screen.getByRole('button', { name: '跳转' }))
    expect(await screen.findByText('第 65 轮结论摘要')).toBeInTheDocument()

    currentRounds = currentRounds.slice(0, 5)
    wsHolder.lastMessage = { type: 'round', data: { round_id: 'id-100', ok: true, wake_source: '定时唤醒' } }
    rerender(timelineUi(''))

    expect(await screen.findByText('共 5 条决策')).toBeInTheDocument()
    expect(await screen.findByText('第 100 轮结论摘要')).toBeInTheDocument()
    expect(screen.getByText('第 1/1 页 · 共 5 条决策')).toBeInTheDocument()
  })

  it('点击卡片仍按需读取审计详情并保留完整对话展示', async () => {
    renderTimeline()
    await screen.findByText('第 100 轮结论摘要')

    // summary 已移出手风琴按钮（避免与 ClampText 展开按钮嵌套），改点摘要行短号
    fireEvent.click(screen.getByText('#100'))
    await waitFor(() => expect(holder.getRound).toHaveBeenCalledWith('id-100'))
    expect(await screen.findByText('get_account')).toBeInTheDocument()
    expect(screen.getByText('工具调用详情 · tool_calls（1 步）')).toBeInTheDocument()
    const chatSummary = screen.getByText(/完整对话 · agent loop/)
    fireEvent.click(chatSummary)
    expect(screen.getByText('RAW-id-100')).toBeInTheDocument()
  })

  it('笔记引文仍取最新笔记页中同轮的第一条记录', async () => {
    currentNotes = [
      { time: '2023-11-14T23:00:00.000Z', content: '最新结论：突破确认再加仓。', round_id: 'id-100' },
      { time: '2023-11-14T22:00:00.000Z', content: '较早记录：先观察量能。', round_id: 'id-100' },
    ]
    renderTimeline()

    expect(await screen.findByText(/最新结论：突破确认再加仓。/)).toBeInTheDocument()
    expect(screen.queryByText(/较早记录：先观察量能。/)).not.toBeInTheDocument()
    expect(holder.getNotes).toHaveBeenCalledWith(0, 20)
  })

  it('焦点目标在其他页时切换到对应页并展开高亮卡片', async () => {
    renderTimeline('id-94')
    await screen.findByText('第 100 轮结论摘要')

    fireEvent.click(screen.getByTestId('focus-btn'))
    expect(await screen.findByText('第 94 轮结论摘要')).toBeInTheDocument()
    await waitFor(() => expect(holder.getRound).toHaveBeenCalledWith('id-94'))
    expect(document.querySelector('[data-round-id="id-94"]')?.classList.contains('jump-hl')).toBe(true)
  })

  it('焦点定位可跨越第 10 页以外的完整历史记录', async () => {
    currentRounds = Array.from({ length: 60 }, (_, index) => ({
      ...ROUNDS[0],
      round_id: `id-${100 - index}`,
      summary: `第 ${100 - index} 轮结论摘要`,
    }))
    renderTimeline('id-46')
    await screen.findByText('第 100 轮结论摘要')

    fireEvent.click(screen.getByTestId('focus-btn'))
    expect(await screen.findByText('第 46 轮结论摘要')).toBeInTheDocument()
    expect(holder.getRounds).toHaveBeenCalledWith(50, 5)
  })

  it('首屏数据加载期间接到焦点请求时，等待数据后再定位而不误报未找到', async () => {
    const initialPage = deferred<RoundsPageResult>()
    holder.getRounds.mockImplementationOnce(() => initialPage.promise)
    renderTimeline('id-100')

    fireEvent.click(screen.getByTestId('focus-btn'))
    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument())

    initialPage.resolve(pageOf(currentRounds, 0, 5))
    expect(await screen.findByText('第 100 轮结论摘要')).toBeInTheDocument()
    await waitFor(() => expect(holder.getRound).toHaveBeenCalledWith('id-100'))
  })

  it('首屏加载完成后仍能继续跨页定位此前收到的焦点请求', async () => {
    const initialPage = deferred<RoundsPageResult>()
    holder.getRounds.mockImplementationOnce(() => initialPage.promise)
    renderTimeline('id-94')

    fireEvent.click(screen.getByTestId('focus-btn'))
    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument())

    initialPage.resolve(pageOf(currentRounds, 0, 5))
    expect(await screen.findByText('第 94 轮结论摘要')).toBeInTheDocument()
    await waitFor(() => expect(holder.getRound).toHaveBeenCalledWith('id-94'))
  })

  it('策略版本标签：按 strategyMd5 join 显示 vN · 来源，无匹配与空串降级为「—」', async () => {
    renderTimeline()
    await screen.findByText('第 100 轮结论摘要')

    // md5-aaa → v2（复盘）：第 100/98 轮各一枚徽标；md5-bbb → v3（人工）：第 99 轮
    expect((await screen.findAllByText('v2 · 复盘')).length).toBe(2)
    expect(screen.getByText('v3 · 人工')).toBeInTheDocument()
    // 第 97 轮 md5-unknown（无匹配）+ 第 96 轮空串：均显示「—」
    expect(screen.getAllByText('—').length).toBe(2)
  })

  it('展开的轮详情同样展示策略版本标签', async () => {
    renderTimeline()
    await screen.findByText('第 100 轮结论摘要')

    fireEvent.click(screen.getByText('#100'))
    await waitFor(() => expect(holder.getRound).toHaveBeenCalledWith('id-100'))
    expect(await screen.findByText('策略版本：v2 · 复盘')).toBeInTheDocument()
  })

  it('WS round 事件一并刷新策略版本表', async () => {
    const { rerender } = renderTimeline()
    await screen.findByText('第 100 轮结论摘要')
    expect(holder.getStrategyVersions).toHaveBeenCalledTimes(1)

    wsHolder.lastMessage = { type: 'round', data: { round_id: 'id-100', ok: true, wake_source: '定时唤醒' } }
    rerender(timelineUi(''))

    await waitFor(() => expect(holder.getStrategyVersions).toHaveBeenCalledTimes(2))
  })

  it('版本表加载失败：顶部给非阻断提示，时间线照常、徽标降级「—」', async () => {
    holder.getStrategyVersions.mockRejectedValue(new Error('boom'))
    renderTimeline()

    expect(await screen.findByText('策略版本表加载失败')).toBeInTheDocument()
    expect(await screen.findByText('第 100 轮结论摘要')).toBeInTheDocument()
    // 版本表不可用：本页 5 张卡片徽标全部降级
    expect(screen.getAllByText('—')).toHaveLength(5)
  })
})
