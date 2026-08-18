/**
 * 复盘进行中进度条测试：WS 事件驱动进出进行中态、面板点火事件（review-round-ignite）
 * 不依赖 WS 激活、面板 409 catchup 事件（review-round-catchup）补漏激活、WS 断线恢复后重跑补漏、
 * 3 秒轮询刷新工具链、轮次绑定（点火后先返回上一轮已结束记录保持等待不误退、见到进行中轮绑定 round_id）、
 * 代际校验（上一激活周期的迟到 /live 响应不关闭新一轮）、点火兜底（约 90 秒不见进行中轮视为点火失败退出）、
 * 双通道结束（WS review_round / 轮询确认本轮 ended_at 非空）停止轮询并回调 onFinished、
 * 挂载补漏（含 30 分钟僵尸轮防线）、轮询失败静默保留进度条。
 */
import { act, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { ReviewLive, WsMessage } from '../api/types'
import ReviewLiveStrip from '../components/console/ReviewLiveStrip'

const holder = vi.hoisted(() => ({
  lastMessage: null as WsMessage | null,
  connected: true,
  getReviewLive: vi.fn<() => Promise<ReviewLive>>(),
}))

vi.mock('../api', () => ({
  api: { getReviewLive: () => holder.getReviewLive() },
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

  it('面板点火事件 review-round-ignite → 不经 WS 直接进入进行中态（WS 断线窗口点火兜底）', async () => {
    await renderStrip(vi.fn())
    expect(screen.queryByTestId('review-live-strip')).not.toBeInTheDocument()

    act(() => {
      window.dispatchEvent(new CustomEvent('review-round-ignite'))
    })

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

  it('点火后 /live 先返回上一轮已结束记录 → 保持等待不退出；随后返回进行中轮 → 绑定并展示工具链', async () => {
    const onFinished = vi.fn()
    await renderStrip(onFinished)
    // 上一轮的历史记录（rv-old，起点火之前就已结束）：新后台任务还没 begin_round 时 /live 返回的就是它
    holder.getReviewLive.mockResolvedValue({ round: endedRound(NOW_S - 3600, NOW_S - 3500, 'rv-old'), tool_calls: [] })
    act(() => {
      window.dispatchEvent(new CustomEvent('review-round-ignite'))
    })
    await act(async () => vi.advanceTimersByTimeAsync(0)) // 激活后立即 pollOnce：返回旧轮已结束
    expect(screen.getByTestId('review-live-strip')).toBeInTheDocument() // 不得误退
    expect(onFinished).not.toHaveBeenCalled()
    await act(async () => vi.advanceTimersByTimeAsync(3000)) // 再轮询仍是旧轮：继续等待
    expect(screen.getByTestId('review-live-strip')).toBeInTheDocument()
    expect(onFinished).not.toHaveBeenCalled()

    // 新后台任务 begin_round 完成：/live 返回进行中轮 → 绑定轮次并展示工具链
    holder.getReviewLive.mockResolvedValue({ round: liveRound(NOW_S, 'rv-new'), tool_calls: TWO_CALLS })
    await act(async () => vi.advanceTimersByTimeAsync(3000))
    expect(screen.getByText('复盘进行中 · 已调用 2 个工具 · 最近：get_decision_detail')).toBeInTheDocument()
    expect(onFinished).not.toHaveBeenCalled()
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

  it('面板 409 catchup 事件 review-round-catchup → 补漏发现进行中轮并激活（WS 断线场景）', async () => {
    holder.connected = false // 本页 WS 断线：start 事件收不到
    await renderStrip(vi.fn())
    expect(screen.queryByTestId('review-live-strip')).not.toBeInTheDocument()

    // 别的标签页/自动调度已点火：/live 可见进行中轮
    holder.getReviewLive.mockResolvedValue({ round: liveRound(NOW_S - 10, 'rv-else'), tool_calls: TWO_CALLS })
    act(() => {
      window.dispatchEvent(new CustomEvent('review-round-catchup'))
    })
    await act(async () => vi.advanceTimersByTimeAsync(0))

    expect(screen.getByText('复盘进行中 · 已调用 2 个工具 · 最近：get_decision_detail')).toBeInTheDocument()
  })

  it('点火后约 90 秒 /live 仍只返回上一轮已结束记录 → 视为后台点火失败，退出并回调 onFinished', async () => {
    const onFinished = vi.fn()
    await renderStrip(onFinished)
    holder.getReviewLive.mockResolvedValue({ round: endedRound(NOW_S - 3600, NOW_S - 3500, 'rv-old'), tool_calls: [] })
    act(() => {
      window.dispatchEvent(new CustomEvent('review-round-ignite'))
    })
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
})
