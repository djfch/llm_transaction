/**
 * 仪表盘测试：数据渲染（mock API）+ kill_switch 两段确认切换流程。
 */
import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import DashboardPage from '../pages/DashboardPage'

// jsdom 无法运行 lightweight-charts（依赖 canvas），替换为占位组件
vi.mock('../components/EquityChart', () => ({
  default: () => <div data-testid="equity-chart-stub" />,
}))

// 隔离 WS：测试中不需要真实推送
vi.mock('../hooks/useWs', () => ({
  useWs: () => ({ connected: true, lastMessage: null }),
}))

describe('仪表盘 DashboardPage', () => {
  it('渲染账户概览、持仓与运行状态（字段为 变量名(含义) 格式）', async () => {
    render(<DashboardPage />)

    // 账户概览：标签遵循 变量名(含义) 规范
    expect(await screen.findByText('equity(账户权益 USDT)')).toBeInTheDocument()
    expect(screen.getByText('available(可用余额 USDT)')).toBeInTheDocument()
    expect(screen.getByText('unrealised_pnl(未实现盈亏 USDT)')).toBeInTheDocument()
    // mock 权益值
    expect(screen.getByText('10,842.36')).toBeInTheDocument()

    // 持仓卡片（两个持仓各有一张，字段标签重复出现属预期）
    expect(await screen.findByText('BTC_USDT')).toBeInTheDocument()
    expect(screen.getByText('ETH_USDT')).toBeInTheDocument()
    expect(screen.getAllByText('entry_price(开仓均价)')).toHaveLength(2)

    // 运行状态
    expect(await screen.findByText('mode(运行模式)')).toBeInTheDocument()
    expect(screen.getByText('paper')).toBeInTheDocument()
    expect(screen.getByText('未触发')).toBeInTheDocument()
  })

  it('kill_switch 需两次点击确认，切换后状态刷新', async () => {
    render(<DashboardPage />)

    const openBtn = await screen.findByRole('button', { name: /开启 kill_switch/ })
    // 第一次点击：进入待确认状态，尚未真正开启
    fireEvent.click(openBtn)
    const confirmBtn = await screen.findByRole('button', { name: /再次点击确认开启/ })
    // 第二次点击：真正切换，状态刷新为已触发
    fireEvent.click(confirmBtn)

    expect(await screen.findByRole('button', { name: /关闭 kill_switch/ })).toBeInTheDocument()
    expect(await screen.findByText('已触发')).toBeInTheDocument()

    // 恢复：再次两段确认关闭，避免影响其他用例共享的 mock 状态
    fireEvent.click(screen.getByRole('button', { name: /关闭 kill_switch/ }))
    fireEvent.click(await screen.findByRole('button', { name: /再次点击确认关闭/ }))
    expect(await screen.findByRole('button', { name: /开启 kill_switch/ })).toBeInTheDocument()
  })
})
