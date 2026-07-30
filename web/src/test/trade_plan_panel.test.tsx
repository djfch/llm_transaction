/** 交易计划面板测试：有计划渲染全文+更新时间、空态、加载失败、refreshKey 变化重拉。 */
import { render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { TradePlan } from '../api/types'
import TradePlanPanel from '../components/console/TradePlanPanel'

const holder = vi.hoisted(() => ({ getPlan: vi.fn() }))
vi.mock('../api', () => ({ api: { getPlan: () => holder.getPlan() } }))

const PLAN: TradePlan = {
  content: '## BTC 做空\n入场：反弹至 64200-64300 受阻\n止损：64500',
  roundId: 'r-1',
  updatedAt: '2026-07-20T02:30:00Z',
}

beforeEach(() => {
  vi.clearAllMocks()
  holder.getPlan.mockResolvedValue(PLAN)
})

describe('TradePlanPanel(交易计划)', () => {
  it('有计划：渲染标题、全文与更新时间徽标', async () => {
    render(<TradePlanPanel refreshKey={0} />)
    expect(screen.getByText('交易计划 · trade_plan')).toBeInTheDocument()
    expect(await screen.findByText(/反弹至 64200-64300 受阻/)).toBeInTheDocument()
    expect(screen.getByText(/更新于/)).toBeInTheDocument()
  })

  it('空计划（content 空串）：显示空态，不显示更新时间', async () => {
    holder.getPlan.mockResolvedValue({ content: '', roundId: '', updatedAt: null })
    render(<TradePlanPanel refreshKey={0} />)
    expect(await screen.findByText('暂无数据')).toBeInTheDocument()
    expect(screen.queryByText(/更新于/)).not.toBeInTheDocument()
  })

  it('加载失败：显示错误提示', async () => {
    holder.getPlan.mockRejectedValue(new Error('boom'))
    render(<TradePlanPanel refreshKey={0} />)
    expect(await screen.findByText(/加载失败：boom/)).toBeInTheDocument()
  })

  it('refreshKey 变化触发重拉（决策轮更新计划后刷新）', async () => {
    const { rerender } = render(<TradePlanPanel refreshKey={0} />)
    await screen.findByText(/反弹至 64200-64300 受阻/)
    expect(holder.getPlan).toHaveBeenCalledTimes(1)

    holder.getPlan.mockResolvedValue({
      content: '## ETH 做多\n入场：回踩 1900',
      roundId: 'r-2',
      updatedAt: '2026-07-20T03:00:00Z',
    })
    rerender(<TradePlanPanel refreshKey={1} />)
    await waitFor(() => expect(holder.getPlan).toHaveBeenCalledTimes(2))
    expect(await screen.findByText(/回踩 1900/)).toBeInTheDocument()
  })
})
