/**
 * 仪表盘测试：数据渲染（mock API）+ kill_switch 两段确认切换流程 + 布局结构。
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { mockApi } from '../api/mock'
import DashboardPage from '../pages/DashboardPage'

// 显式使用 mock 假数据（api/index.ts 默认已反转为真实后端，测试不能依赖默认值）
vi.mock('../api', () => ({ api: mockApi }))

// jsdom 无法运行 lightweight-charts（依赖 canvas），替换为占位组件
vi.mock('../components/EquityChart', () => ({
  default: () => <div data-testid="equity-chart-stub" />,
}))
vi.mock('../components/CandleChart', () => ({
  default: () => <div data-testid="candle-chart-stub" />,
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
    // 注：K 线卡的合约下拉也会出现 BTC_USDT/ETH_USDT 文本，故用 getAllByText
    expect((await screen.findAllByText('BTC_USDT')).length).toBeGreaterThan(0)
    expect(screen.getAllByText('ETH_USDT').length).toBeGreaterThan(0)
    expect(screen.getAllByText('entry_price(开仓均价)')).toHaveLength(2)

    // 运行状态（含 agent_running(运行状态) 徽标）
    expect(await screen.findByText('mode(运行模式)')).toBeInTheDocument()
    expect(screen.getByText('paper')).toBeInTheDocument()
    expect(screen.getByText('未触发')).toBeInTheDocument()
    expect(screen.getByText('agent_running(运行状态)')).toBeInTheDocument()
    expect(screen.getByText('运行中')).toBeInTheDocument()
    // mock 默认 llm_configured=true：无未配置横幅，徽标为已配置
    expect(screen.queryByText(/LLM 未配置：/)).not.toBeInTheDocument()
    expect(screen.getByText('llm_configured(LLM配置)')).toBeInTheDocument()
    expect(screen.getByText('已配置')).toBeInTheDocument()
  })

  it('llm_configured=false 时页面顶部显示琥珀色提示横幅', async () => {
    // 仅本用例桩掉 getStatus，返回 LLM 未配置状态
    const spy = vi.spyOn(mockApi, 'getStatus').mockResolvedValue({
      mode: 'paper',
      uptime_seconds: 3600,
      kill_switch: false,
      llm_provider: 'anthropic',
      llm_model: 'claude-sonnet-4-5',
      llm_configured: false,
      agent_running: true,
    })
    try {
      render(<DashboardPage />)
      expect(
        await screen.findByText(
          'LLM 未配置：监控与手动操作可用，自动决策已暂停。请到配置中心设置 LLM API Key。',
        ),
      ).toBeInTheDocument()
      // 运行状态卡同步显示未配置徽标
      expect(screen.getByText('llm_configured(LLM配置)')).toBeInTheDocument()
      expect(screen.getByText('未配置')).toBeInTheDocument()
    } finally {
      spy.mockRestore()
    }
  })

  it('kill_switch 需两次点击确认，切换后状态刷新', async () => {
    render(<DashboardPage />)

    const openBtn = await screen.findByRole('button', { name: /开启 kill_switch/ })
    // status 未加载完成时按钮禁用（防止按错误初始状态操作），先等其可用
    await waitFor(() => expect(openBtn).toBeEnabled())
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

  it('布局为行情决策优先：行1 账户+状态 / 行2 K线+实时决策 / 行3 持仓+权益与笔记', async () => {
    render(<DashboardPage />)

    // 行 1：账户指标与运行状态卡同行
    const row1 = screen.getByTestId('row-account-status')
    expect(await within(row1).findByText('equity(账户权益 USDT)')).toBeInTheDocument()
    expect(within(row1).getByText('agent_running(运行状态)')).toBeInTheDocument()

    // 行 2：K 线卡与实时决策卡同行
    const row2 = screen.getByTestId('row-market-decision')
    expect(within(row2).getByText(/interval\(周期\)/)).toBeInTheDocument()
    expect(
      (await within(row2).findAllByText(/决策中…|上轮决策|暂无决策记录/)).length
    ).toBeGreaterThan(0)

    // 行 3：持仓在左、权益小图与最近笔记在右列
    const row3 = screen.getByTestId('row-positions-secondary')
    expect(within(row3).getByText('当前持仓 positions')).toBeInTheDocument()
    expect(within(row3).getByText('权益曲线 equity')).toBeInTheDocument()
    expect(within(row3).getByText('最近笔记 notes')).toBeInTheDocument()
  })
})
