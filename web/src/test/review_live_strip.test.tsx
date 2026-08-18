/**
 * 复盘进行中进度条测试（按 ID 认轮的状态机）：WS 事件驱动进出进行中态、面板点火事件
 * （review-round-ignite，detail.roundId 预分配轮 ID）pinned 绑定、面板 409 catchup 事件
 * （review-round-catchup）discovery 轮询发现、WS 断线恢复后重跑补漏、3 秒轮询刷新工具链、
 * pinned 只认绑定轮（僵尸轮/其他轮忽略不换绑）、discovery 见更新轮换绑、
 * 本轮快速结束按 ID 立即识别（不比较时间，覆盖两端时钟偏差）、WS 轮末事件 round_id 不符不退出、
 * 代际校验（上一激活周期的迟到 /live 响应不关闭新一轮）、90 秒兜底（绑定轮从未出现视为点火失败退出）、
 * 挂载补漏（含 30 分钟僵尸轮防线、补漏绑定后能被更新轮换绑退出、绑定轮变僵尸认定死亡退出）、
 * pinned 按绑定 ID 直查（?round_id=）：绑定轮已被见后 /live 最新轮被别轮占位仍直查自己 ID 识别结束、
 * 绑定轮超 30 分钟未闭合（进程重启残留）僵尸判定退出、直查恒查无此轮走 90 秒兜底、
 * 轮询失败静默保留进度条、已激活时 catchup 与 WS 重连补漏不降级 pinned。
 */
import { act, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { ReviewLive, WsMessage } from '../api/types'
import ReviewLiveStrip from '../components/console/ReviewLiveStrip'

const holder = vi.hoisted(() => ({
  lastMessage: null as WsMessage | null,
  connected: true,
  getReviewLive: vi.fn<(roundId?: string) => Promise<ReviewLive>>(),
}))

vi.mock('../api', () => ({
  api: { getReviewLive: (roundId?: string) => holder.getReviewLive(roundId) },
}))

vi.mock('../hooks/useWs', () => ({
  useWs: () => ({ connected: holder.connected, lastMessage: holder.lastMessage }),
}))

const NOW_S = 1_784_600_000 // 固定「现在」（Unix 秒），配合 setSystemTime 保证僵尸轮判定确定

/** 进行中的复盘轮（ended_at 为 null；roundId 可覆盖）。 */
function liveRound(startedAt: number, roundId = 'rv-1'): NonNullable<ReviewLive['round']> {
  return {
    round_id: roundId,
    wake_source: 'review',
    prompt_md5: 'md5',
    prompt_snapshot: 'prompt',
    context_snapshot: 'ctx',
    llm_raw: '',
    strategy_md5: 's-md5',
    started_at: startedAt,
    ended_at: null,
    error: '',
  }
}

/** 已结束的复盘轮（roundId 可覆盖）：旧轮历史记录 / 本轮快速结束等场景用。 */
function endedRound(startedAt: number, endedAt: number, roundId = 'rv-1'): NonNullable<ReviewLive['round']> {
  return { ...liveRound(startedAt, roundId), ended_at: endedAt }
}

/** 两条复盘工具调用（最近一条为 get_decision_detail；result 按后端 {text} 包装形状）。 */
const TWO_CALLS: ReviewLive['tool_calls'] = [
  { seq: 1, tool: 'get_review_stats', args: { interval_days: 1 }, risk_verdict: '', risk_reason: '', result: { text: '概览' }, duration_ms: 12 },
  { seq: 2, tool: 'get_decision_detail', args: { round_id: 'round-0037' }, risk_verdict: '', risk_reason: '', result: { text: '明细' }, duration_ms: 9 },
]

/** 另一条工具链（最近一条为 get_round_detail）：discovery 换绑后区分新旧轮的工具链展示。 */
const ALT_CALLS: ReviewLive['tool_calls'] = [
  { seq: 1, tool: 'get_round_detail', args: { round_id: 'round-0042' }, risk_verdict: '', risk_reason: '', result: { text: '轮次' }, duration_ms: 7 },
]

/** 派发面板点火事件（新形态：detail 携带 POST 预分配的 roundId）。 */
function ignite(roundId: string) {
  act(() => {
    window.dispatchEvent(new CustomEvent('review-round-ignite', { detail: { roundId } }))
  })
}

/** 按入参分流的 /live mock（贴生产语义）：带参但 id 与 byId 轮次不符即查无此轮（round null），不带参返回 latest。 */
function mockLiveRouting(byId: ReviewLive, latest: ReviewLive) {
  holder.getReviewLive.mockImplementation((roundId) => {
    if (roundId) return Promise.resolve(roundId === byId.round?.round_id ? byId : { round: null, tool_calls: [] })
    return Promise.resolve(latest)
  })
}

/** 渲染并冲刷挂载补漏请求（默认 mock 返回 round=null，不进进行中态）。 */
async function renderStrip(onFinished: () => void) {
  const utils = render(<ReviewLiveStrip onFinished={onFinished} />)
  await act(async () => vi.advanceTimersByTimeAsync(0))
  return utils
}

beforeEach(() => {
  vi.useFakeTimers()
  vi.setSystemTime(NOW_S * 1000)
  holder.lastMessage = null
  holder.connected = true
  holder.getReviewLive.mockReset()
  holder.getReviewLive.mockResolvedValue({ round: null, tool_calls: [] })
})

afterEach(() => {
  vi.useRealTimers()
})

describe('ReviewLiveStrip(复盘进行中进度条)', () => {
  it('收到 review_round_start → 进度条出现、显示等待文案并开始每 3 秒轮询', async () => {
    const onFinished = vi.fn()
    const { rerender } = await renderStrip(onFinished)
    expect(screen.queryByTestId('review-live-strip')).not.toBeInTheDocument()

    holder.lastMessage = { type: 'review_round_start', data: { round_id: 'rv-1' } }
    rerender(<ReviewLiveStrip onFinished={onFinished} />)
    await act(async () => vi.advanceTimersByTimeAsync(0))

    expect(screen.getByTestId('review-live-strip')).toBeInTheDocument()
    expect(screen.getByText('复盘进行中 · 等待 LLM 发起调用…')).toBeInTheDocument()
    expect(screen.getByText('每 3 秒自动刷新')).toBeInTheDocument()

    const callsBefore = holder.getReviewLive.mock.calls.length
    await act(async () => vi.advanceTimersByTimeAsync(3000))
    expect(holder.getReviewLive.mock.calls.length).toBeGreaterThan(callsBefore)
  })

  it('轮询返回进行中轮与 2 条工具调用 → 显示已调用数量与最近工具名', async () => {
    const onFinished = vi.fn()
    const { rerender } = await renderStrip(onFinished)
    holder.lastMessage = { type: 'review_round_start', data: { round_id: 'rv-1' } }
    rerender(<ReviewLiveStrip onFinished={onFinished} />)
    await act(async () => vi.advanceTimersByTimeAsync(0))

    holder.getReviewLive.mockResolvedValue({ round: liveRound(NOW_S - 18), tool_calls: TWO_CALLS })
    await act(async () => vi.advanceTimersByTimeAsync(3000))

    expect(screen.getByText('复盘进行中 · 已调用 2 个工具 · 最近：get_decision_detail')).toBeInTheDocument()
  })

  it('收到 review_round → 进度条消失、onFinished 被调一次、轮询停止', async () => {
    const onFinished = vi.fn()
    const { rerender } = await renderStrip(onFinished)
    holder.lastMessage = { type: 'review_round_start', data: { round_id: 'rv-1' } }
    rerender(<ReviewLiveStrip onFinished={onFinished} />)
    await act(async () => vi.advanceTimersByTimeAsync(0))
    expect(screen.getByTestId('review-live-strip')).toBeInTheDocument()

    holder.lastMessage = { type: 'review_round', data: { round_id: 'rv-1', ok: true } }
    rerender(<ReviewLiveStrip onFinished={onFinished} />)
    await act(async () => vi.advanceTimersByTimeAsync(0))

    expect(screen.queryByTestId('review-live-strip')).not.toBeInTheDocument()
    expect(onFinished).toHaveBeenCalledTimes(1)
    const callsAfterFinish = holder.getReviewLive.mock.calls.length
    await act(async () => vi.advanceTimersByTimeAsync(9000))
    expect(holder.getReviewLive.mock.calls.length).toBe(callsAfterFinish)
  })

  it('WS 轮末事件 round_id 与绑定不符 → 不退出（防御其他轮的迟到事件误杀本周期）', async () => {
    const onFinished = vi.fn()
    const { rerender } = await renderStrip(onFinished)
    holder.lastMessage = { type: 'review_round_start', data: { round_id: 'rv-1' } }
    rerender(<ReviewLiveStrip onFinished={onFinished} />)
    await act(async () => vi.advanceTimersByTimeAsync(0))
    expect(screen.getByTestId('review-live-strip')).toBeInTheDocument()

    // 其他轮的轮末事件：忽略，不退出不通知
    holder.lastMessage = { type: 'review_round', data: { round_id: 'rv-other', ok: true } }
    rerender(<ReviewLiveStrip onFinished={onFinished} />)
    await act(async () => vi.advanceTimersByTimeAsync(3000))
    expect(screen.getByTestId('review-live-strip')).toBeInTheDocument()
    expect(onFinished).not.toHaveBeenCalled()

    // 绑定轮的轮末事件：正常退出
    holder.lastMessage = { type: 'review_round', data: { round_id: 'rv-1', ok: true } }
    rerender(<ReviewLiveStrip onFinished={onFinished} />)
    await act(async () => vi.advanceTimersByTimeAsync(0))
    expect(screen.queryByTestId('review-live-strip')).not.toBeInTheDocument()
    expect(onFinished).toHaveBeenCalledTimes(1)
  })

  it('轮询发现 ended_at 非空 → 进度条消失并回调 onFinished（WS 结束事件丢失的兜底）', async () => {
    const onFinished = vi.fn()
    holder.getReviewLive.mockResolvedValue({ round: liveRound(NOW_S - 18), tool_calls: TWO_CALLS })
    await renderStrip(onFinished)
    expect(screen.getByTestId('review-live-strip')).toBeInTheDocument()

    holder.getReviewLive.mockResolvedValue({
      round: endedRound(NOW_S - 18, NOW_S),
      tool_calls: TWO_CALLS,
    })
    await act(async () => vi.advanceTimersByTimeAsync(3000))

    expect(screen.queryByTestId('review-live-strip')).not.toBeInTheDocument()
    expect(onFinished).toHaveBeenCalledTimes(1)
  })

  it('挂载时已有进行中轮 → 无需 WS 事件直接出现进度条', async () => {
    holder.getReviewLive.mockResolvedValue({ round: liveRound(NOW_S - 120), tool_calls: TWO_CALLS })
    await renderStrip(vi.fn())
    expect(screen.getByText('复盘进行中 · 已调用 2 个工具 · 最近：get_decision_detail')).toBeInTheDocument()
  })

  it('挂载时 started_at 超 30 分钟的僵尸轮 → 不出现进度条', async () => {
    holder.getReviewLive.mockResolvedValue({ round: liveRound(NOW_S - 31 * 60), tool_calls: TWO_CALLS })
    await renderStrip(vi.fn())
    expect(screen.queryByTestId('review-live-strip')).not.toBeInTheDocument()
  })

  it('轮询请求失败 → 静默保留进度条，不闪错误', async () => {
    holder.getReviewLive.mockResolvedValue({ round: liveRound(NOW_S - 18), tool_calls: TWO_CALLS })
    await renderStrip(vi.fn())
    expect(screen.getByTestId('review-live-strip')).toBeInTheDocument()

    holder.getReviewLive.mockRejectedValueOnce(new Error('网络抖动'))
    await act(async () => vi.advanceTimersByTimeAsync(3000))
    expect(screen.getByTestId('review-live-strip')).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('面板点火事件 review-round-ignite（detail 带 roundId）→ 不经 WS 直接 pinned 进入进行中态', async () => {
    await renderStrip(vi.fn())
    expect(screen.queryByTestId('review-live-strip')).not.toBeInTheDocument()

    ignite('rv-new')

    expect(screen.getByTestId('review-live-strip')).toBeInTheDocument()
    expect(screen.getByText('复盘进行中 · 等待 LLM 发起调用…')).toBeInTheDocument()
  })

  it('WS 由断开恢复为连接 → 重跑补漏查询，找回断线期间自动点火的进行中轮', async () => {
    const { rerender } = await renderStrip(vi.fn())
    expect(screen.queryByTestId('review-live-strip')).not.toBeInTheDocument()
    const callsAtMount = holder.getReviewLive.mock.calls.length
    expect(callsAtMount).toBeGreaterThan(0) // 挂载补漏已查一次

    // 断线期间自动调度点火（补漏数据源返进行中轮），但 start 事件随断线丢失
    holder.connected = false
    rerender(<ReviewLiveStrip onFinished={vi.fn()} />)
    holder.getReviewLive.mockResolvedValue({ round: liveRound(NOW_S - 20), tool_calls: TWO_CALLS })

    holder.connected = true
    rerender(<ReviewLiveStrip onFinished={vi.fn()} />)
    await act(async () => vi.advanceTimersByTimeAsync(0))

    expect(holder.getReviewLive.mock.calls.length).toBeGreaterThan(callsAtMount)
    expect(screen.getByTestId('review-live-strip')).toBeInTheDocument()
    expect(screen.getByText('复盘进行中 · 已调用 2 个工具 · 最近：get_decision_detail')).toBeInTheDocument()
  })

  it('pinned 点火后无参 /live 仍返回上一轮已结束记录、直查绑定轮暂查无此轮 → 保持等待不误退；随后绑定轮进行中 → 展示工具链；其结束 → 退出', async () => {
    const onFinished = vi.fn()
    await renderStrip(onFinished)
    // 上一轮的历史记录（rv-old，点火之前就已结束）：新后台任务还没 begin_round 时无参 /live 返回的就是它，
    // 而按 rv-new 直查查无此轮（后端契约：round null）
    const oldEnded: ReviewLive = { round: endedRound(NOW_S - 3600, NOW_S - 3500, 'rv-old'), tool_calls: [] }
    mockLiveRouting({ round: null, tool_calls: [] }, oldEnded)
    ignite('rv-new')
    await act(async () => vi.advanceTimersByTimeAsync(0)) // 激活后立即 pollOnce：直查 rv-new 查无此轮，继续等待
    expect(screen.getByTestId('review-live-strip')).toBeInTheDocument() // 不得误退
    expect(onFinished).not.toHaveBeenCalled()
    await act(async () => vi.advanceTimersByTimeAsync(3000)) // 再轮询仍查无此轮：继续等待
    expect(screen.getByTestId('review-live-strip')).toBeInTheDocument()
    expect(onFinished).not.toHaveBeenCalled()

    // 新后台任务 begin_round 完成：直查 rv-new 返回进行中轮 → 展示工具链
    mockLiveRouting({ round: liveRound(NOW_S, 'rv-new'), tool_calls: TWO_CALLS }, oldEnded)
    await act(async () => vi.advanceTimersByTimeAsync(3000))
    expect(screen.getByText('复盘进行中 · 已调用 2 个工具 · 最近：get_decision_detail')).toBeInTheDocument()
    expect(onFinished).not.toHaveBeenCalled()

    // 绑定轮结束 → 退出并通知
    mockLiveRouting({ round: endedRound(NOW_S, NOW_S + 30, 'rv-new'), tool_calls: TWO_CALLS }, oldEnded)
    await act(async () => vi.advanceTimersByTimeAsync(3000))
    expect(screen.queryByTestId('review-live-strip')).not.toBeInTheDocument()
    expect(onFinished).toHaveBeenCalledTimes(1)
  })

  it('pinned 点火后无参 /live 返回僵尸进行中轮、直查绑定轮暂查无此轮 → 不展示僵尸轮工具链、不退出；绑定轮出现后正常走完', async () => {
    const onFinished = vi.fn()
    await renderStrip(onFinished)
    // 上次强杀残留的未闭合旧轮（started_at 超 30 分钟，ended_at=null）：pinned 按 ID 直查根本查不到它
    const zombie: ReviewLive = { round: liveRound(NOW_S - 40 * 60, 'rv-zombie'), tool_calls: TWO_CALLS }
    mockLiveRouting({ round: null, tool_calls: [] }, zombie)
    ignite('rv-new')
    await act(async () => vi.advanceTimersByTimeAsync(0))
    expect(screen.getByTestId('review-live-strip')).toBeInTheDocument()
    expect(screen.getByText('复盘进行中 · 等待 LLM 发起调用…')).toBeInTheDocument() // 不展示僵尸轮工具链
    expect(onFinished).not.toHaveBeenCalled()
    await act(async () => vi.advanceTimersByTimeAsync(3000)) // 僵尸轮持续占位：直查绑定轮仍查无此轮，不退出
    expect(screen.getByText('复盘进行中 · 等待 LLM 发起调用…')).toBeInTheDocument()
    expect(onFinished).not.toHaveBeenCalled()

    // 本轮开始：直查 rv-new 返回进行中轮 → 展示其工具链
    mockLiveRouting({ round: liveRound(NOW_S, 'rv-new'), tool_calls: TWO_CALLS }, zombie)
    await act(async () => vi.advanceTimersByTimeAsync(3000))
    expect(screen.getByText('复盘进行中 · 已调用 2 个工具 · 最近：get_decision_detail')).toBeInTheDocument()

    // 本轮结束 → 退出并通知（僵尸轮占位不干扰，兜底也不会误伤）
    mockLiveRouting({ round: endedRound(NOW_S, NOW_S + 30, 'rv-new'), tool_calls: TWO_CALLS }, zombie)
    await act(async () => vi.advanceTimersByTimeAsync(3000))
    expect(screen.queryByTestId('review-live-strip')).not.toBeInTheDocument()
    expect(onFinished).toHaveBeenCalledTimes(1)
  })

  it('本轮快速结束（started_at 早于点火时刻，模拟时钟偏差/服务端先跑）→ 按 ID 立即识别退出，不等 90 秒', async () => {
    const onFinished = vi.fn()
    await renderStrip(onFinished)
    // 服务端本轮 10 分钟前就已跑完：started_at/ended_at 都早于点火时刻，旧时间比较语义会误判为历史轮
    holder.getReviewLive.mockResolvedValue({
      round: endedRound(NOW_S - 600, NOW_S - 590, 'rv-fast'),
      tool_calls: TWO_CALLS,
    })
    ignite('rv-fast')
    await act(async () => vi.advanceTimersByTimeAsync(0)) // 首次轮询即见绑定轮已结束 → 立即退出

    expect(screen.queryByTestId('review-live-strip')).not.toBeInTheDocument()
    expect(onFinished).toHaveBeenCalledTimes(1)
  })

  it('面板 409 catchup 事件 → discovery 激活：/live 为空保持等待，进行中轮出现即绑定，其结束即退出', async () => {
    const onFinished = vi.fn()
    holder.connected = false // 本页 WS 断线：start 事件收不到
    await renderStrip(onFinished)
    expect(screen.queryByTestId('review-live-strip')).not.toBeInTheDocument()

    // 任务已预留但审计轮还没开：此刻 /live 为空；discovery 也要立即激活（不等一次性探测）
    act(() => {
      window.dispatchEvent(new CustomEvent('review-round-catchup'))
    })
    await act(async () => vi.advanceTimersByTimeAsync(0))
    expect(screen.getByTestId('review-live-strip')).toBeInTheDocument()
    expect(screen.getByText('复盘进行中 · 等待 LLM 发起调用…')).toBeInTheDocument()
    expect(onFinished).not.toHaveBeenCalled() // /live 仍为空：不退出
    await act(async () => vi.advanceTimersByTimeAsync(3000))
    expect(onFinished).not.toHaveBeenCalled()

    // 审计轮开始：/live 出现进行中轮 → 绑定并展示工具链
    holder.getReviewLive.mockResolvedValue({ round: liveRound(NOW_S - 10, 'rv-else'), tool_calls: TWO_CALLS })
    await act(async () => vi.advanceTimersByTimeAsync(3000))
    expect(screen.getByText('复盘进行中 · 已调用 2 个工具 · 最近：get_decision_detail')).toBeInTheDocument()

    // 该轮结束 → 退出并通知
    holder.getReviewLive.mockResolvedValue({ round: endedRound(NOW_S - 10, NOW_S + 20, 'rv-else'), tool_calls: TWO_CALLS })
    await act(async () => vi.advanceTimersByTimeAsync(3000))
    expect(screen.queryByTestId('review-live-strip')).not.toBeInTheDocument()
    expect(onFinished).toHaveBeenCalledTimes(1)
  })

  it('discovery 换绑：绑定 A 后 /live 返回更新的进行中轮 B → 改绑 B 并展示其工具链；B 结束 → 退出', async () => {
    const onFinished = vi.fn()
    await renderStrip(onFinished)
    holder.getReviewLive.mockResolvedValue({ round: liveRound(NOW_S - 60, 'rv-a'), tool_calls: TWO_CALLS })
    act(() => {
      window.dispatchEvent(new CustomEvent('review-round-catchup'))
    })
    await act(async () => vi.advanceTimersByTimeAsync(0))
    expect(screen.getByText('复盘进行中 · 已调用 2 个工具 · 最近：get_decision_detail')).toBeInTheDocument() // 绑定 A

    // /live 返回更新的进行中轮 B：discovery 模式下 /live 的更新轮次总是当前真相 → 换绑
    holder.getReviewLive.mockResolvedValue({ round: liveRound(NOW_S - 5, 'rv-b'), tool_calls: ALT_CALLS })
    await act(async () => vi.advanceTimersByTimeAsync(3000))
    expect(screen.getByText('复盘进行中 · 已调用 1 个工具 · 最近：get_round_detail')).toBeInTheDocument() // 展示 B 的工具链
    expect(onFinished).not.toHaveBeenCalled()

    // B 结束 → 退出并通知（A 的遗留记录不再干扰）
    holder.getReviewLive.mockResolvedValue({ round: endedRound(NOW_S - 5, NOW_S + 10, 'rv-b'), tool_calls: ALT_CALLS })
    await act(async () => vi.advanceTimersByTimeAsync(3000))
    expect(screen.queryByTestId('review-live-strip')).not.toBeInTheDocument()
    expect(onFinished).toHaveBeenCalledTimes(1)
  })

  it('挂载补漏绑定近期未闭合轮后，新轮出现能换绑退出（防回归：discovery 绑定不再永久卡死）', async () => {
    const onFinished = vi.fn()
    // 挂载时 /live 返回 5 分钟前开始的未闭合轮（<30 分钟非僵尸，补漏绑定；事后证明是强杀残留）
    holder.getReviewLive.mockResolvedValue({ round: liveRound(NOW_S - 300, 'rv-stale'), tool_calls: TWO_CALLS })
    await renderStrip(onFinished)
    expect(screen.getByText('复盘进行中 · 已调用 2 个工具 · 最近：get_decision_detail')).toBeInTheDocument()

    // 新轮（rv-new）开始：discovery 绑定可换绑到更新轮
    holder.getReviewLive.mockResolvedValue({ round: liveRound(NOW_S - 3, 'rv-new'), tool_calls: ALT_CALLS })
    await act(async () => vi.advanceTimersByTimeAsync(3000))
    expect(screen.getByText('复盘进行中 · 已调用 1 个工具 · 最近：get_round_detail')).toBeInTheDocument()

    // 新轮结束 → 正常退出（不会因误绑旧轮而卡死）
    holder.getReviewLive.mockResolvedValue({ round: endedRound(NOW_S - 3, NOW_S + 20, 'rv-new'), tool_calls: ALT_CALLS })
    await act(async () => vi.advanceTimersByTimeAsync(3000))
    expect(screen.queryByTestId('review-live-strip')).not.toBeInTheDocument()
    expect(onFinished).toHaveBeenCalledTimes(1)
  })

  it('上一激活周期发出的 /live 请求在新周期激活后才返回 → 迟到响应被丢弃，新一轮不被关闭', async () => {
    const onFinished = vi.fn()
    const { rerender } = await renderStrip(onFinished)
    // 第一周期：WS start 激活，激活后的首次 pollOnce 挂起（响应迟到）
    let resolveStale!: (live: ReviewLive) => void
    holder.getReviewLive.mockImplementationOnce(
      () => new Promise<ReviewLive>((resolve) => { resolveStale = resolve }),
    )
    holder.lastMessage = { type: 'review_round_start', data: { round_id: 'rv-1' } }
    rerender(<ReviewLiveStrip onFinished={onFinished} />)
    await act(async () => vi.advanceTimersByTimeAsync(0))
    expect(screen.getByTestId('review-live-strip')).toBeInTheDocument()

    // 第一周期经 WS 结束，第二周期随即开始（代际递增）
    holder.lastMessage = { type: 'review_round', data: { round_id: 'rv-1', ok: true } }
    rerender(<ReviewLiveStrip onFinished={onFinished} />)
    await act(async () => vi.advanceTimersByTimeAsync(0))
    expect(onFinished).toHaveBeenCalledTimes(1)
    holder.lastMessage = { type: 'review_round_start', data: { round_id: 'rv-2' } }
    rerender(<ReviewLiveStrip onFinished={onFinished} />)
    await act(async () => vi.advanceTimersByTimeAsync(0))
    expect(screen.getByTestId('review-live-strip')).toBeInTheDocument()

    // 第一周期的迟到响应此刻才返回（rv-1 已结束）：不得关闭第二周期
    await act(async () => {
      resolveStale({ round: endedRound(NOW_S - 60, NOW_S - 5, 'rv-1'), tool_calls: TWO_CALLS })
    })
    expect(screen.getByTestId('review-live-strip')).toBeInTheDocument()
    expect(onFinished).toHaveBeenCalledTimes(1) // 未重复触发
  })

  it('pinned 点火后约 90 秒绑定轮从未在 /live 出现（无参 /live 始终只有旧已结束轮、直查恒查无此轮）→ 兜底退出并回调 onFinished', async () => {
    const onFinished = vi.fn()
    await renderStrip(onFinished)
    mockLiveRouting({ round: null, tool_calls: [] }, { round: endedRound(NOW_S - 3600, NOW_S - 3500, 'rv-old'), tool_calls: [] })
    ignite('rv-new')
    await act(async () => vi.advanceTimersByTimeAsync(0))
    expect(screen.getByTestId('review-live-strip')).toBeInTheDocument()

    // 未到兜底期限（60 秒）：持续等待不退出
    await act(async () => vi.advanceTimersByTimeAsync(60_000))
    expect(screen.getByTestId('review-live-strip')).toBeInTheDocument()
    expect(onFinished).not.toHaveBeenCalled()

    // 越过约 90 秒兜底期限 → 退出并通知（列表刷新出失败报告）
    await act(async () => vi.advanceTimersByTimeAsync(31_000))
    expect(screen.queryByTestId('review-live-strip')).not.toBeInTheDocument()
    expect(onFinished).toHaveBeenCalledTimes(1)
  })

  it('discovery 绑定轮绑定后变僵尸（进程被强杀、永不闭合且无新轮）→ 认定死亡退出并回调 onFinished', async () => {
    const onFinished = vi.fn()
    // 挂载补漏绑定 5 分钟前开始的未闭合轮（<30 分钟非僵尸，seen=true）
    holder.getReviewLive.mockResolvedValue({ round: liveRound(NOW_S - 300, 'rv-stale'), tool_calls: TWO_CALLS })
    await renderStrip(onFinished)
    expect(screen.getByText('复盘进行中 · 已调用 2 个工具 · 最近：get_decision_detail')).toBeInTheDocument()

    // 26 分钟后轮龄越过 30 分钟僵尸线且始终未闭合（/live 仍返回它）：绑定轮变僵尸 → 死亡退出，
    // 否则 seen=true 后兜底永不触发、状态条永久卡死在「进行中」
    vi.setSystemTime((NOW_S + 26 * 60) * 1000)
    await act(async () => vi.advanceTimersByTimeAsync(3000))

    expect(screen.queryByTestId('review-live-strip')).not.toBeInTheDocument()
    expect(onFinished).toHaveBeenCalledTimes(1)
  })

  it('pinned 周期内收到 409 catchup 事件（复点按钮）→ 不降级为 discovery：绑定与工具链保持', async () => {
    const onFinished = vi.fn()
    await renderStrip(onFinished)
    holder.getReviewLive.mockResolvedValue({ round: liveRound(NOW_S - 10, 'rv-new'), tool_calls: TWO_CALLS })
    ignite('rv-new')
    await act(async () => vi.advanceTimersByTimeAsync(0))
    expect(screen.getByText('复盘进行中 · 已调用 2 个工具 · 最近：get_decision_detail')).toBeInTheDocument()

    // 复点按钮收到 409 → catchup 事件：已激活守卫不重建周期（降级会立即清空工具链）
    act(() => {
      window.dispatchEvent(new CustomEvent('review-round-catchup'))
    })
    expect(screen.getByText('复盘进行中 · 已调用 2 个工具 · 最近：get_decision_detail')).toBeInTheDocument()
    expect(onFinished).not.toHaveBeenCalled()

    // /live 无参出现另一进行中轮：若守卫失守降级为 discovery 会换绑它；pinned 直查 rv-new 仍只返回本轮
    mockLiveRouting(
      { round: liveRound(NOW_S - 10, 'rv-new'), tool_calls: TWO_CALLS },
      { round: liveRound(NOW_S - 5, 'rv-other'), tool_calls: ALT_CALLS },
    )
    await act(async () => vi.advanceTimersByTimeAsync(3000))
    expect(screen.getByText('复盘进行中 · 已调用 2 个工具 · 最近：get_decision_detail')).toBeInTheDocument()
    expect(onFinished).not.toHaveBeenCalled()
    // pinned 直查以绑定 ID 发起，未退化为无参查询
    expect(holder.getReviewLive).toHaveBeenCalledWith('rv-new')

    // 绑定轮结束 → 正常退出
    mockLiveRouting(
      { round: endedRound(NOW_S - 10, NOW_S + 20, 'rv-new'), tool_calls: TWO_CALLS },
      { round: liveRound(NOW_S - 5, 'rv-other'), tool_calls: ALT_CALLS },
    )
    await act(async () => vi.advanceTimersByTimeAsync(3000))
    expect(screen.queryByTestId('review-live-strip')).not.toBeInTheDocument()
    expect(onFinished).toHaveBeenCalledTimes(1)
  })

  it('pinned 激活中 WS 断开后重连 → 重连补漏不降级 pinned（activeRef 守卫）', async () => {
    const onFinished = vi.fn()
    const { rerender } = await renderStrip(onFinished)
    holder.getReviewLive.mockResolvedValue({ round: liveRound(NOW_S - 10, 'rv-new'), tool_calls: TWO_CALLS })
    ignite('rv-new')
    await act(async () => vi.advanceTimersByTimeAsync(0))
    expect(screen.getByText('复盘进行中 · 已调用 2 个工具 · 最近：get_decision_detail')).toBeInTheDocument()

    // WS 断开→重连：触发一次性补漏；此刻无参 /live 返回另一轮，守卫失守会被换绑降级（直查 rv-new 仍返回本轮）
    holder.connected = false
    rerender(<ReviewLiveStrip onFinished={onFinished} />)
    mockLiveRouting(
      { round: liveRound(NOW_S - 10, 'rv-new'), tool_calls: TWO_CALLS },
      { round: liveRound(NOW_S - 5, 'rv-other'), tool_calls: ALT_CALLS },
    )
    holder.connected = true
    rerender(<ReviewLiveStrip onFinished={onFinished} />)
    await act(async () => vi.advanceTimersByTimeAsync(0))

    // 守卫生效：绑定与工具链不被改写
    expect(screen.getByText('复盘进行中 · 已调用 2 个工具 · 最近：get_decision_detail')).toBeInTheDocument()
    expect(onFinished).not.toHaveBeenCalled()
  })

  it('pinned 绑定轮已被见后结束、无参 /live 最新轮已被别轮占位 → 直查绑定 ID 仍识别结束退出（场景一防回归）', async () => {
    const onFinished = vi.fn()
    await renderStrip(onFinished)
    // 直查 rv-new 返回进行中绑定轮；无参 /live 已被 rv-other 占位（模拟 WS 断线期间别处点火的新轮）
    mockLiveRouting(
      { round: liveRound(NOW_S - 20, 'rv-new'), tool_calls: TWO_CALLS },
      { round: liveRound(NOW_S - 5, 'rv-other'), tool_calls: ALT_CALLS },
    )
    ignite('rv-new')
    await act(async () => vi.advanceTimersByTimeAsync(0))
    expect(screen.getByText('复盘进行中 · 已调用 2 个工具 · 最近：get_decision_detail')).toBeInTheDocument() // seen=true
    await act(async () => vi.advanceTimersByTimeAsync(3000))
    expect(onFinished).not.toHaveBeenCalled()

    // 绑定轮结束：直查 rv-new 返回已结束（最新轮仍被 rv-other 占位）→ 正常退出；
    // 旧实现 pinned 只看不带参的最新轮，seen=true 后兜底永不触发，会永久卡「进行中」
    mockLiveRouting(
      { round: endedRound(NOW_S - 20, NOW_S, 'rv-new'), tool_calls: TWO_CALLS },
      { round: liveRound(NOW_S - 5, 'rv-other'), tool_calls: ALT_CALLS },
    )
    await act(async () => vi.advanceTimersByTimeAsync(3000))
    expect(screen.queryByTestId('review-live-strip')).not.toBeInTheDocument()
    expect(onFinished).toHaveBeenCalledTimes(1)
  })

  it('pinned 绑定轮已被见后超 30 分钟未闭合（进程重启残留脏轮）→ 僵尸判定退出并回调 onFinished（场景二防回归）', async () => {
    const onFinished = vi.fn()
    await renderStrip(onFinished)
    // 直查与无参都返回 5 分钟前开始的未闭合绑定轮（轮龄 <30 分钟，非僵尸，seen=true）
    const bound: ReviewLive = { round: liveRound(NOW_S - 300, 'rv-new'), tool_calls: TWO_CALLS }
    mockLiveRouting(bound, bound)
    ignite('rv-new')
    await act(async () => vi.advanceTimersByTimeAsync(0))
    expect(screen.getByText('复盘进行中 · 已调用 2 个工具 · 最近：get_decision_detail')).toBeInTheDocument()

    // 26 分钟后轮龄越过 30 分钟僵尸线且始终未闭合（进程重启，ended_at 永远为 null）：
    // pinned 直查命中僵尸 → 认定死亡退出；旧实现 pinned 分支无僵尸判定，会永久卡「进行中」
    vi.setSystemTime((NOW_S + 26 * 60) * 1000)
    await act(async () => vi.advanceTimersByTimeAsync(3000))
    expect(screen.queryByTestId('review-live-strip')).not.toBeInTheDocument()
    expect(onFinished).toHaveBeenCalledTimes(1)
  })

  it('pinned 直查恒查无绑定轮（后台从未 begin_round）、无参 /live 另有进行中轮 → 90 秒兜底退出并回调 onFinished', async () => {
    const onFinished = vi.fn()
    await renderStrip(onFinished)
    // 直查 rv-new 恒查无此轮；无参 /live 有另一进行中轮（与纯空 /live 的 90 秒兜底用例区分 mock 行为）
    mockLiveRouting(
      { round: null, tool_calls: [] },
      { round: liveRound(NOW_S - 10, 'rv-other'), tool_calls: ALT_CALLS },
    )
    ignite('rv-new')
    await act(async () => vi.advanceTimersByTimeAsync(0))
    expect(screen.getByTestId('review-live-strip')).toBeInTheDocument()

    // 未到兜底期限（60 秒）：持续等待不退出，也不展示他轮工具链
    await act(async () => vi.advanceTimersByTimeAsync(60_000))
    expect(screen.getByTestId('review-live-strip')).toBeInTheDocument()
    expect(screen.getByText('复盘进行中 · 等待 LLM 发起调用…')).toBeInTheDocument()
    expect(onFinished).not.toHaveBeenCalled()

    // 越过约 90 秒兜底期限 → 退出并通知（列表刷新出失败报告）
    await act(async () => vi.advanceTimersByTimeAsync(31_000))
    expect(screen.queryByTestId('review-live-strip')).not.toBeInTheDocument()
    expect(onFinished).toHaveBeenCalledTimes(1)
  })
})
