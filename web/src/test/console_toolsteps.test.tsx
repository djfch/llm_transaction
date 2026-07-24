/**
 * ToolSteps 测试：allow/deny 徽标、deny 风控理由行内展示、空态等待提示、
 * 入参超长截断可展开、结果折叠区交互、未入风控（空串）免判徽标。
 */
import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { ToolCall } from '../api/types'
import ToolSteps from '../components/console/ToolSteps'

/** 工具调用夹具：默认 allow 判定，各用例按需覆盖字段 */
function call(over: Partial<ToolCall> = {}): ToolCall {
  return {
    seq: 1,
    tool: 'get_account',
    args: {},
    risk_verdict: 'allow',
    risk_reason: '',
    result: { text: 'equity=10842.36' },
    duration_ms: 12,
    ...over,
  }
}

describe('ToolSteps 工具调用步骤链', () => {
  it('空数组：显示「等待 LLM 发起调用…」', () => {
    render(<ToolSteps toolCalls={[]} />)
    expect(screen.getByText('等待 LLM 发起调用…')).toBeInTheDocument()
  })

  it('allow 判定：绿色放行徽标 + 工具名 + 耗时右对齐展示', () => {
    render(<ToolSteps toolCalls={[call()]} />)
    expect(screen.getByText('风控放行')).toBeInTheDocument()
    expect(screen.getByText('get_account')).toBeInTheDocument()
    expect(screen.getByText('seq 1')).toBeInTheDocument()
    expect(screen.getByText('12ms')).toBeInTheDocument()
  })

  it('deny 判定：红色拒绝徽标 + risk_reason 行内直接展示（无需展开）', () => {
    render(
      <ToolSteps
        toolCalls={[
          call({
            seq: 4,
            tool: 'place_order',
            risk_verdict: 'deny',
            risk_reason: '下单后单仓占权益 36% > max_position_pct 30%',
          }),
        ]}
      />,
    )
    expect(screen.getByText('风控拒绝')).toBeInTheDocument()
    expect(screen.getByText(/风控理由：下单后单仓占权益 36%/)).toBeInTheDocument()
    expect(screen.queryByText(/deny\(|risk_reason\(/)).not.toBeInTheDocument()
  })

  it('空串判定（未入风控）：显示「免判(未入风控)」而非 deny', () => {
    render(<ToolSteps toolCalls={[call({ risk_verdict: '' })]} />)
    expect(screen.getByText('免判(未入风控)')).toBeInTheDocument()
    expect(screen.queryByText('风控拒绝')).not.toBeInTheDocument()
  })

  it('入参超长截断显示省略号，点击「展开」显示全文', () => {
    render(<ToolSteps toolCalls={[call({ args: { content: 'x'.repeat(200) } })]} />)
    // 截断后不存在 200 个连续 x
    expect(screen.queryByText(/x{200}/)).not.toBeInTheDocument()
    fireEvent.click(screen.getByText('展开'))
    expect(screen.getByText(/x{200}/)).toBeInTheDocument()
    // 再次点击可收起
    fireEvent.click(screen.getByText('收起'))
    expect(screen.queryByText(/x{200}/)).not.toBeInTheDocument()
  })

  it('结果折叠区：默认收起，点击摘要展开 <pre> 查看结果', () => {
    const { container } = render(<ToolSteps toolCalls={[call()]} />)
    const details = container.querySelector('details')!
    expect(details).not.toHaveAttribute('open')
    fireEvent.click(screen.getByText(/执行结果/))
    expect(details).toHaveAttribute('open')
    expect(screen.getByText(/equity=10842\.36/)).toBeInTheDocument()
    expect(screen.queryByText(/result\(/)).not.toBeInTheDocument()
  })

  it('多步渲染：按 seq 展示圆点步骤，仅最后一步无连线', () => {
    const { container } = render(
      <ToolSteps
        toolCalls={[
          call(),
          call({ seq: 2, tool: 'place_order', risk_verdict: 'deny', risk_reason: '日下单超限' }),
        ]}
      />,
    )
    expect(screen.getByText('seq 1')).toBeInTheDocument()
    expect(screen.getByText('seq 2')).toBeInTheDocument()
    // deny 步骤圆点为 ✕，allow 为 ✓
    expect(screen.getByText('✕')).toBeInTheDocument()
    expect(screen.getByText('✓')).toBeInTheDocument()
    expect(container.querySelectorAll('li')).toHaveLength(2)
  })
})
