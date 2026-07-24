/**
 * ConversationThread 测试：anthropic 格式 llm_raw 渲染出 assistant/user 消息流、
 * deny 结果红色标记与风控理由、折叠区交互（默认收起/defaultOpen 展开）、空输入不渲染。
 */
import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { ToolCall } from '../api/types'
import ConversationThread from '../components/console/ConversationThread'

// Anthropic 原生格式 llm_raw：两个 assistant 回合按 \n 连接（首回合 text+两个 tool_use，末回合纯文本结论）
const ANTHROPIC_RAW = [
  JSON.stringify({
    role: 'assistant',
    content: [
      { type: 'text', text: '先查账户与 K 线。' },
      { type: 'tool_use', id: 't1', name: 'get_account', input: {} },
      { type: 'tool_use', id: 't2', name: 'place_order', input: { contract: 'BTC_USDT', size: 20 } },
    ],
  }),
  JSON.stringify({
    role: 'assistant',
    content: [{ type: 'text', text: '加仓被风控拒绝，维持现有持仓。' }],
  }),
].join('\n')

/** 审计工具调用链（与 llm_raw 的 tool_use 按序对应；place_order 被风控拒绝） */
const AUDIT: ToolCall[] = [
  {
    seq: 1,
    tool: 'get_account',
    args: {},
    risk_verdict: '',
    risk_reason: '',
    result: 'equity=10842.36',
    duration_ms: 10,
  },
  {
    seq: 2,
    tool: 'place_order',
    args: { contract: 'BTC_USDT', size: 20 },
    risk_verdict: 'deny',
    risk_reason: '单仓超限 36% > 30%',
    result: '风控拒绝，未下单',
    duration_ms: 8,
  },
]

describe('ConversationThread 完整对话消息流', () => {
  it('anthropic llm_raw：渲染 assistant 文本/发起调用消息与 user 工具返回消息', () => {
    render(<ConversationThread llmRaw={ANTHROPIC_RAW} toolCalls={AUDIT} defaultOpen />)
    // assistant 思考/结论文本
    expect(screen.getByText('先查账户与 K 线。')).toBeInTheDocument()
    expect(screen.getByText('加仓被风控拒绝，维持现有持仓。')).toBeInTheDocument()
    // assistant 发起调用（工具名 + 参数摘要）
    expect(screen.getByText(/发起调用 place_order/)).toBeInTheDocument()
    // user·工具返回内容
    expect(screen.getByText(/equity=10842\.36/)).toBeInTheDocument()
    expect(screen.getByText('USER · 工具返回 get_account')).toBeInTheDocument()
    // 标题徽标消息数：2 文本 + 2 调用 + 2 返回 = 6
    expect(screen.getByText('6 条消息')).toBeInTheDocument()
    // assistant 消息卡共 4 张
    expect(screen.getAllByText('ASSISTANT')).toHaveLength(4)
  })

  it('deny 的工具返回：红色系卡片 + 「（风控拒绝）」标记 + 中文风控理由标签', () => {
    render(<ConversationThread llmRaw={ANTHROPIC_RAW} toolCalls={AUDIT} defaultOpen />)
    const label = screen.getByText(/（风控拒绝）/)
    expect(label).toBeInTheDocument()
    // 所在卡片为红色系（deny 标记）
    expect(label.parentElement!.className).toContain('rose')
    expect(screen.getByText(/风控理由：单仓超限 36% > 30%/)).toBeInTheDocument()
    expect(screen.queryByText(/risk_reason\(/)).not.toBeInTheDocument()
  })

  it('折叠交互：默认收起，点击摘要展开；defaultOpen 时初始展开', () => {
    const { container, unmount } = render(
      <ConversationThread llmRaw={ANTHROPIC_RAW} toolCalls={AUDIT} />,
    )
    const details = container.querySelector('details')!
    expect(details).not.toHaveAttribute('open')
    fireEvent.click(screen.getByText(/完整对话 · agent loop/))
    expect(details).toHaveAttribute('open')
    unmount()
    const again = render(<ConversationThread llmRaw={ANTHROPIC_RAW} toolCalls={AUDIT} defaultOpen />)
    expect(again.container.querySelector('details')!).toHaveAttribute('open')
  })

  it('空输入（llm_raw 与 toolCalls 均无消息）：不渲染任何内容', () => {
    const { container } = render(<ConversationThread llmRaw="" toolCalls={[]} />)
    expect(container).toBeEmptyDOMElement()
  })
})
