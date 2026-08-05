/**
 * 硬性风控速览面板测试：api.getConfig() 取 risk 段，四行参数（比例 ×100 显示）
 * + 底部裁决说明；加载/失败走 StateHint。
 */
import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { AppConfig } from '../api/types'
import RiskPanel from '../components/console/RiskPanel'

const CONFIG: AppConfig = {
  mode: 'paper',
  llm: {
    provider: 'anthropic',
    model: 'claude-sonnet-4-5',
    max_tokens: 4096,
    openai_base_url: '',
    thinking_effort: '',
    max_consecutive_failures: 3,
  },
  risk: {
    max_position_pct: 0.3,
    max_total_position_pct: 0.8,
    max_leverage: 5,
    daily_loss_limit: 0.1,
    max_orders_per_day: 20,
    max_deviation: 0.02,
    kill_switch: false,
  },
  scheduler: { default_wake_minutes: 60, min_wake_minutes: 5, max_wake_minutes: 720 },
  notify: { telegram_enabled: false },
}

const holder = vi.hoisted(() => ({
  getConfig: vi.fn<() => Promise<AppConfig>>(() => Promise.resolve(CONFIG)),
}))
vi.mock('../api', () => ({ api: { getConfig: () => holder.getConfig() } }))

describe('RiskPanel(硬性风控 · 代码保证)', () => {
  it('渲染四行风控参数（比例 ×100 百分比）+ 裁决说明', async () => {
    render(<RiskPanel />)
    expect(screen.getByText('硬性风控 · 代码保证')).toBeInTheDocument()
    expect(await screen.findByText('单仓上限 max_position_pct')).toBeInTheDocument()
    expect(screen.getByText('30%')).toBeInTheDocument()
    expect(screen.getByText('总仓上限 max_total_position_pct')).toBeInTheDocument()
    expect(screen.getByText('80%')).toBeInTheDocument()
    expect(screen.getByText('价格偏离 max_deviation')).toBeInTheDocument()
    expect(screen.getByText('2%')).toBeInTheDocument()
    expect(screen.getByText('日亏锁仓 daily_loss_limit')).toBeInTheDocument()
    const loss = screen.getByText('10%')
    expect(loss.className).toContain('text-rose-300') // 日亏锁仓警示色
    expect(screen.getByText(/LLM 仅有建议权/)).toBeInTheDocument()
    expect(holder.getConfig).toHaveBeenCalledTimes(1)
  })

  it('取数失败 → StateHint 错误态', async () => {
    holder.getConfig.mockRejectedValueOnce(new Error('boom'))
    render(<RiskPanel />)
    expect(await screen.findByText(/加载失败：boom/)).toBeInTheDocument()
  })
})
