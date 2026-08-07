/**
 * 研报进行中进度条测试：WS 事件驱动进出进行中态、3 秒轮询刷新工具链、
 * 双通道结束（WS research_round / 轮询发现 ended_at 非空）停止轮询并回调 onFinished、
 * 挂载补漏（含 30 分钟僵尸轮防线）、轮询失败静默保留进度条。
 */
import { act, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { ResearchLive, WsMessage } from '../api/types'
import ResearchLiveStrip from '../components/console/ResearchLiveStrip'

const holder = vi.hoisted(() => ({
  lastMessage: null as WsMessage | null,
  getResearchLive: vi.fn<() => Promise<ResearchLive>>(),
}))

vi.mock('../api', () => ({
  api: { getResearchLive: () => holder.getResearchLive() },
}))

vi.mock('../hooks/useWs', () => ({
  useWs: () => ({ connected: true, lastMessage: holder.lastMessage }),
}))

const NOW_S = 1_784_600_000 // 固定「现在」（Unix 秒），配合 setSystemTime 保证僵尸轮判定确定

/** 进行中的研报轮（ended_at 为 null；形状与复盘轮一致，wake_source=research）。 */
function liveRound(startedAt: number): NonNullable<ResearchLive['round']> {
  return {
    round_id: 'rs-1',
    wake_source: 'research',
    prompt_md5: 'md5',
    prompt_snapshot: 'prompt',
    context_snapshot: 'ctx',
    llm_raw: '',
    started_at: startedAt,
    ended_at: null,
    error: '',
  }
}

/** 两条研报工具调用（最近一条为 get_news_flash；result 按后端 {text} 包装形状）。 */
const TWO_CALLS: ResearchLive['tool_calls'] = [
  { seq: 1, tool: 'get_macro_context', args: { hours: 24 }, risk_verdict: '', risk_reason: '', result: { text: '概览' }, duration_ms: 12 },
  { seq: 2, tool: 'get_news_flash', args: { keyword: 'ETF' }, risk_verdict: '', risk_reason: '', result: { text: '明细' }, duration_ms: 9 },
]

/** 渲染并冲刷挂载补漏请求（默认 mock 返回 round=null，不进进行中态）。 */
async function renderStrip(onFinished: () => void) {
  const utils = render(<ResearchLiveStrip onFinished={onFinished} />)
  await act(async () => vi.advanceTimersByTimeAsync(0))
  return utils
}

beforeEach(() => {
  vi.useFakeTimers()
  vi.setSystemTime(NOW_S * 1000)
  holder.lastMessage = null
  holder.getResearchLive.mockReset()
  holder.getResearchLive.mockResolvedValue({ round: null, tool_calls: [] })
})

afterEach(() => {
  vi.useRealTimers()
})

describe('ResearchLiveStrip(研报进行中进度条)', () => {
  it('收到 research_round_start → 进度条出现、显示等待文案并开始每 3 秒轮询', async () => {
    const onFinished = vi.fn()
    const { rerender } = await renderStrip(onFinished)
    expect(screen.queryByTestId('research-live-strip')).not.toBeInTheDocument()

    holder.lastMessage = { type: 'research_round_start', data: { round_id: 'rs-1' } }
    rerender(<ResearchLiveStrip onFinished={onFinished} />)
    await act(async () => vi.advanceTimersByTimeAsync(0))

    expect(screen.getByTestId('research-live-strip')).toBeInTheDocument()
    expect(screen.getByText('研报生成中 · 等待 LLM 发起调用…')).toBeInTheDocument()
    expect(screen.getByText('每 3 秒自动刷新')).toBeInTheDocument()

    const callsBefore = holder.getResearchLive.mock.calls.length
    await act(async () => vi.advanceTimersByTimeAsync(3000))
    expect(holder.getResearchLive.mock.calls.length).toBeGreaterThan(callsBefore)
  })

  it('轮询返回进行中轮与 2 条工具调用 → 显示已调用数量与最近工具名', async () => {
    const onFinished = vi.fn()
    const { rerender } = await renderStrip(onFinished)
    holder.lastMessage = { type: 'research_round_start', data: { round_id: 'rs-1' } }
    rerender(<ResearchLiveStrip onFinished={onFinished} />)
    await act(async () => vi.advanceTimersByTimeAsync(0))

    holder.getResearchLive.mockResolvedValue({ round: liveRound(NOW_S - 18), tool_calls: TWO_CALLS })
    await act(async () => vi.advanceTimersByTimeAsync(3000))

    expect(screen.getByText('研报生成中 · 已调用 2 个工具 · 最近：get_news_flash')).toBeInTheDocument()
  })

  it('收到 research_round → 进度条消失、onFinished 被调一次、轮询停止', async () => {
    const onFinished = vi.fn()
    const { rerender } = await renderStrip(onFinished)
    holder.lastMessage = { type: 'research_round_start', data: { round_id: 'rs-1' } }
    rerender(<ResearchLiveStrip onFinished={onFinished} />)
    await act(async () => vi.advanceTimersByTimeAsync(0))
    expect(screen.getByTestId('research-live-strip')).toBeInTheDocument()

    holder.lastMessage = { type: 'research_round', data: { round_id: 'rs-1', ok: true } }
    rerender(<ResearchLiveStrip onFinished={onFinished} />)
    await act(async () => vi.advanceTimersByTimeAsync(0))

    expect(screen.queryByTestId('research-live-strip')).not.toBeInTheDocument()
    expect(onFinished).toHaveBeenCalledTimes(1)
    const callsAfterFinish = holder.getResearchLive.mock.calls.length
    await act(async () => vi.advanceTimersByTimeAsync(9000))
    expect(holder.getResearchLive.mock.calls.length).toBe(callsAfterFinish)
  })

  it('轮询发现 ended_at 非空 → 进度条消失并回调 onFinished（WS 结束事件丢失的兜底）', async () => {
    const onFinished = vi.fn()
    holder.getResearchLive.mockResolvedValue({ round: liveRound(NOW_S - 18), tool_calls: TWO_CALLS })
    await renderStrip(onFinished)
    expect(screen.getByTestId('research-live-strip')).toBeInTheDocument()

    holder.getResearchLive.mockResolvedValue({
      round: { ...liveRound(NOW_S - 18), ended_at: NOW_S },
      tool_calls: TWO_CALLS,
    })
    await act(async () => vi.advanceTimersByTimeAsync(3000))

    expect(screen.queryByTestId('research-live-strip')).not.toBeInTheDocument()
    expect(onFinished).toHaveBeenCalledTimes(1)
  })

  it('挂载时已有进行中轮 → 无需 WS 事件直接出现进度条', async () => {
    holder.getResearchLive.mockResolvedValue({ round: liveRound(NOW_S - 120), tool_calls: TWO_CALLS })
    await renderStrip(vi.fn())
    expect(screen.getByText('研报生成中 · 已调用 2 个工具 · 最近：get_news_flash')).toBeInTheDocument()
  })

  it('挂载时 started_at 超 30 分钟的僵尸轮 → 不出现进度条', async () => {
    holder.getResearchLive.mockResolvedValue({ round: liveRound(NOW_S - 31 * 60), tool_calls: TWO_CALLS })
    await renderStrip(vi.fn())
    expect(screen.queryByTestId('research-live-strip')).not.toBeInTheDocument()
  })

  it('轮询请求失败 → 静默保留进度条，不闪错误', async () => {
    holder.getResearchLive.mockResolvedValue({ round: liveRound(NOW_S - 18), tool_calls: TWO_CALLS })
    await renderStrip(vi.fn())
    expect(screen.getByTestId('research-live-strip')).toBeInTheDocument()

    holder.getResearchLive.mockRejectedValueOnce(new Error('网络抖动'))
    await act(async () => vi.advanceTimersByTimeAsync(3000))
    expect(screen.getByTestId('research-live-strip')).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })
})
