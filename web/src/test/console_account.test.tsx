/**
 * 账户面板测试：equity 大数字 + 累计涨跌行（正绿 ▲ / 负红 ▼ / 缺省不渲染）
 * + 底部「今日已实现 / 当日开仓单」行（着色与降级，口径与后端 /api/daily_stats 一致）；
 * 回归：paper 权益重置已挪入配置抽屉，面板内不再出现「设置金额」。
 */
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { AccountInfo } from '../api/types'
import AccountPanel from '../components/console/AccountPanel'

const ACCOUNT: AccountInfo = { equity: 10284.56, available: 9216.36, unrealised_pnl: 133.13 }
const DAILY = { realized_pnl: 41.37, orders_today: 7, max_orders_per_day: 20 }

describe('AccountPanel(账户面板)', () => {
  it('加载中：account 为 null 显示占位', () => {
    render(<AccountPanel account={null} mode="paper" />)
    expect(screen.getByText('加载中…')).toBeInTheDocument()
  })

  it('累计涨跌行：正数 ▲ 绿色；权益重置不再内嵌', () => {
    render(<AccountPanel account={ACCOUNT} mode="paper" equityChangePct={2.8456} />)
    expect(screen.getByText('账户 · 模拟盘')).toBeInTheDocument()
    expect(screen.getByText('账户权益')).toBeInTheDocument()
    expect(screen.getByText('可用余额')).toBeInTheDocument()
    expect(screen.getByText('未实现盈亏')).toBeInTheDocument()
    expect(screen.queryByText(/equity\(|available\(|unrealised_pnl/)).not.toBeInTheDocument()
    const line = screen.getByText(/· 累计/)
    expect(line.textContent).toBe('▲ +2.85% · 累计')
    expect(line.className).toContain('text-emerald-400')
    // 回归：权益重置已挪入配置抽屉
    expect(screen.queryByRole('button', { name: /设置金额/ })).not.toBeInTheDocument()
  })

  it('累计涨跌行：负数 ▼ 红色；undefined 不渲染', () => {
    const { unmount } = render(<AccountPanel account={ACCOUNT} mode="paper" equityChangePct={-1.234} />)
    const line = screen.getByText(/· 累计/)
    expect(line.textContent).toBe('▼ -1.23% · 累计')
    expect(line.className).toContain('text-rose-400')
    unmount()
    render(<AccountPanel account={ACCOUNT} mode="paper" />)
    expect(screen.queryByText(/· 累计/)).not.toBeInTheDocument()
  })

  it('底部行：今日已实现正数绿色 + 当日开仓单 n/limit', () => {
    render(<AccountPanel account={ACCOUNT} mode="paper" dailyStats={DAILY} />)
    const realized = screen.getByText('+41.37')
    expect(realized.className).toContain('text-emerald-400')
    expect(screen.getByText('今日已实现')).toBeInTheDocument()
    expect(screen.getByText('当日开仓单')).toBeInTheDocument()
    expect(screen.getByText('7')).toBeInTheDocument()
    expect(screen.getByText('/20')).toBeInTheDocument()
  })

  it('底部行：今日已实现负数红色', () => {
    render(
      <AccountPanel
        account={ACCOUNT}
        mode="paper"
        dailyStats={{ realized_pnl: -12.5, orders_today: 3, max_orders_per_day: 20 }}
      />,
    )
    const realized = screen.getByText('-12.50')
    expect(realized.className).toContain('text-rose-400')
  })

  it('空态降级：dailyStats 为 null 时底部行整体不渲染', () => {
    render(<AccountPanel account={ACCOUNT} mode="paper" dailyStats={null} />)
    expect(screen.queryByText('今日已实现')).not.toBeInTheDocument()
    expect(screen.queryByText('当日开仓单')).not.toBeInTheDocument()
  })
})
