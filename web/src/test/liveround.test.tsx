/**
 * 实时决策卡测试：进行中（徽标/轮询/工具调用追加）、完成态（上轮决策/llm_raw 展开）、
 * 空态（round=null），以及 ToolCallItem 折叠展开交互。
 */
import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import LiveRoundCard from '../components/LiveRoundCard'
import ToolCallItem from '../components/ToolCallItem'
import type { AgentLiveRound, AgentLiveState, ToolCall } from '../api/types'

// 隔离后端与 WS：getAgentLive 由各用例自行控制；wsMessage 模拟 WS 最新消息（验证事件驱动刷新）
const { getAgentLive, wsState } = vi.hoisted(() => ({
  getAgentLive: vi.fn(),
  wsState: { lastMessage: null as import('../api/types').WsMessage | null },
}))
vi.mock('../api', () => ({
  api: { getAgentLive: () => getAgentLive() },
}))
vi.mock('../hooks/useWs', () => ({
  useWs: () => ({ connected: true, lastMessage: wsState.lastMessage }),
}))

afterEach(() => {
  getAgentLive.mockReset()
  wsState.lastMessage = null
})

// ---------- 测试夹具 ----------

const sampleCall: ToolCall = {
  seq: 1,
  tool: 'get_account',
  args: {},
  risk_verdict: 'allow',
  risk_reason: '',
  result: { text: 'equity=10842.36, available=7315.20' },
  duration_ms: 12,
}

const baseRound: AgentLiveRound = {
  round_id: 'abcd1234efgh5678',
  wake_source: '定时唤醒',
  prompt_md5: '9f2c1a3b',
  prompt_snapshot: '# System Prompt\n你是交易 Agent。',
  context_snapshot: '账户权益: 10842.36 USDT',
  llm_raw: '{"thoughts":"观望"}',
  started_at: 1784000000,
  ended_at: 1784000042,
  error: '',
}

/** 进行中：llm_raw 空串、ended_at 为 null，已有 1 次工具调用 */
const liveState: AgentLiveState = {
  in_round: true,
  round: { ...baseRound, llm_raw: '', ended_at: null },
  tool_calls: [sampleCall],
}

/** 完成态：2 次工具调用 */
const doneState: AgentLiveState = {
  in_round: false,
  round: baseRound,
  tool_calls: [sampleCall, { ...sampleCall, seq: 2, tool: 'write_note' }],
}

// ---------- 用例 ----------

describe('实时决策卡 LiveRoundCard', () => {
  it('进行中：显示"决策中…"徽标、等待提示与工具调用列表', async () => {
    getAgentLive.mockResolvedValue(liveState)
    render(<LiveRoundCard />)

    expect(await screen.findByText('决策中…')).toBeInTheDocument()
    // 头部：round_id 短码（前 8 位）+ 唤醒来源
    expect(screen.getByText('#abcd1234')).toBeInTheDocument()
    expect(screen.getByText('wake_source(唤醒来源)=定时唤醒')).toBeInTheDocument()
    // llm_raw 进行中为空串：显示等待提示而非折叠区
    expect(screen.getByText(/等待 LLM 输出…/)).toBeInTheDocument()
    // 工具调用链实时渲染
    expect(screen.getByText('tool_calls(工具调用) · 1 次')).toBeInTheDocument()
    expect(screen.getByText('get_account')).toBeInTheDocument()
  })

  it('进行中：每 3 秒轮询一次 getAgentLive', async () => {
    vi.useFakeTimers()
    try {
      getAgentLive.mockResolvedValue(liveState)
      render(<LiveRoundCard />)
      // 冲刷初次加载（promise 微任务）
      await act(async () => {})
      expect(getAgentLive).toHaveBeenCalledTimes(1)

      await act(async () => {
        vi.advanceTimersByTime(3000)
      })
      expect(getAgentLive).toHaveBeenCalledTimes(2)

      await act(async () => {
        vi.advanceTimersByTime(3000)
      })
      expect(getAgentLive).toHaveBeenCalledTimes(3)
    } finally {
      vi.useRealTimers()
    }
  })

  it('完成态：显示"上轮决策"徽标，llm_raw 折叠区可展开', async () => {
    getAgentLive.mockResolvedValue(doneState)
    render(<LiveRoundCard />)

    expect(await screen.findByText('上轮决策')).toBeInTheDocument()
    expect(screen.queryByText('决策中…')).not.toBeInTheDocument()
    // 工具调用计数与追加的第二次调用
    expect(screen.getByText('tool_calls(工具调用) · 2 次')).toBeInTheDocument()
    expect(screen.getByText('write_note')).toBeInTheDocument()

    // llm_raw 折叠区：默认收起，点击摘要后展开
    const summary = screen.getByText(/llm_raw\(LLM原始输出\)（\d+ 字符）/)
    const details = summary.closest('details')!
    expect(details).not.toHaveAttribute('open')
    fireEvent.click(summary)
    expect(details).toHaveAttribute('open')
  })

  it('空态：round=null 时显示"暂无决策记录"', async () => {
    getAgentLive.mockResolvedValue({ in_round: false, round: null, tool_calls: [] })
    render(<LiveRoundCard />)

    expect(await screen.findByText('暂无决策记录')).toBeInTheDocument()
  })

  it('ToolCallItem：默认收起，点击摘要展开入参与结果', async () => {
    const { container } = render(<ToolCallItem call={sampleCall} />)
    const details = container.querySelector('details')!
    // allow 判定默认收起（deny 才默认展开）
    expect(details).not.toHaveAttribute('open')

    fireEvent.click(screen.getByText('get_account'))
    expect(details).toHaveAttribute('open')
    // 展开后可见入参、结果标签与对象形态结果的 JSON 文本
    expect(screen.getByText('args(调用入参)')).toBeInTheDocument()
    expect(screen.getByText('result(执行结果)')).toBeInTheDocument()
    expect(screen.getByText(/equity=10842\.36/)).toBeInTheDocument()
  })

  // ---------- 风控徽标三态（回归：空串曾被误判为 deny） ----------
  // 非交易工具不过风控引擎，落库 risk_verdict 为空串，此前 !=='allow' 被判成 deny

  it('徽标三态：空串（未入风控）显示"免判(未入风控)"，默认收起', () => {
    const { container } = render(<ToolCallItem call={{ ...sampleCall, risk_verdict: '' }} />)
    expect(screen.getByText('免判(未入风控)')).toBeInTheDocument()
    expect(screen.queryByText('deny(风控拒绝)')).not.toBeInTheDocument()
    expect(container.querySelector('details')!).not.toHaveAttribute('open')
  })

  it('徽标三态：deny 显示"deny(风控拒绝)"，默认展开并露出风控理由', () => {
    const { container } = render(
      <ToolCallItem call={{ ...sampleCall, risk_verdict: 'deny', risk_reason: '单仓超限' }} />,
    )
    expect(screen.getByText('deny(风控拒绝)')).toBeInTheDocument()
    expect(container.querySelector('details')!).toHaveAttribute('open')
    expect(screen.getByText('单仓超限')).toBeInTheDocument()
  })

  it('徽标三态：allow 显示"allow(风控放行)"，默认收起', () => {
    const { container } = render(<ToolCallItem call={sampleCall} />)
    expect(screen.getByText('allow(风控放行)')).toBeInTheDocument()
    expect(container.querySelector('details')!).not.toHaveAttribute('open')
  })

  it('WS round_start(轮开始) 消息触发即时刷新（回归：此前只监听 round，稳态看不到"决策中"）', async () => {
    getAgentLive.mockResolvedValue(doneState)
    const { rerender } = render(<LiveRoundCard />)
    await screen.findByText('上轮决策')
    expect(getAgentLive).toHaveBeenCalledTimes(1)

    // 轮开始事件到达 → 立即重新取数（此时后端返回进行中态）
    getAgentLive.mockResolvedValue(liveState)
    wsState.lastMessage = { type: 'round_start', data: { wake_source: 'timer:60min' } }
    rerender(<LiveRoundCard />)

    expect(await screen.findByText('决策中…')).toBeInTheDocument()
    expect(getAgentLive).toHaveBeenCalledTimes(2)
  })

  it('WS round(轮结束) 消息同样触发刷新', async () => {
    getAgentLive.mockResolvedValue(liveState)
    const { rerender } = render(<LiveRoundCard />)
    await screen.findByText('决策中…')
    const callsBefore = getAgentLive.mock.calls.length

    getAgentLive.mockResolvedValue(doneState)
    wsState.lastMessage = { type: 'round', data: doneState.round as never }
    rerender(<LiveRoundCard />)

    expect(await screen.findByText('上轮决策')).toBeInTheDocument()
    expect(getAgentLive.mock.calls.length).toBe(callsBefore + 1)
  })
})
