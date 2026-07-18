/**
 * AgentControl 测试（M1 回归）：启动单击生效、停止两段确认、失败展示原因。
 * onToggle 直接以 prop 注入，无需 mock api 模块。
 */
import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import AgentControl from '../components/AgentControl'

describe('AgentControl(agent 启停)', () => {
  it('启动：单击即调用 onToggle(true)，无需二次确认', async () => {
    const onToggle = vi.fn().mockResolvedValue(undefined)
    render(<AgentControl running={false} onToggle={onToggle} />)

    fireEvent.click(screen.getByRole('button', { name: '启动 agent(交易代理)' }))
    expect(onToggle).toHaveBeenCalledTimes(1)
    expect(onToggle).toHaveBeenCalledWith(true)
    // 不存在确认中间态
    expect(screen.queryByRole('button', { name: /再次点击/ })).not.toBeInTheDocument()
  })

  it('停止：需两段确认，第一次点击不发请求', async () => {
    const onToggle = vi.fn().mockResolvedValue(undefined)
    render(<AgentControl running={true} onToggle={onToggle} />)

    fireEvent.click(screen.getByRole('button', { name: '停止 agent(交易代理)' }))
    expect(await screen.findByRole('button', { name: '再次点击确认停止' })).toBeInTheDocument()
    expect(onToggle).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: '再次点击确认停止' }))
    expect(onToggle).toHaveBeenCalledTimes(1)
    expect(onToggle).toHaveBeenCalledWith(false)
  })

  it('失败：展示错误原因，按钮恢复可操作', async () => {
    const onToggle = vi.fn().mockRejectedValue(new Error('后端不可用'))
    render(<AgentControl running={false} onToggle={onToggle} />)

    fireEvent.click(screen.getByRole('button', { name: '启动 agent(交易代理)' }))
    expect(await screen.findByText(/后端不可用/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '启动 agent(交易代理)' })).toBeEnabled()
  })

  it('disabled：status 未加载时按钮禁用', () => {
    const onToggle = vi.fn().mockResolvedValue(undefined)
    render(<AgentControl running={false} disabled onToggle={onToggle} />)

    fireEvent.click(screen.getByRole('button', { name: '启动 agent(交易代理)' }))
    expect(onToggle).not.toHaveBeenCalled()
  })
})
