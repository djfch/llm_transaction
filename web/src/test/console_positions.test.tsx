/**
 * 持仓面板测试：空态、多/空卡片渲染（方向徽标/杠杆/字段/盈亏着色）、手动平仓按钮存在。
 * 不测网络：不触发平仓请求（两段确认第一段本就不发请求，此处仅断言按钮存在）。
 */
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { Position } from '../api/types'
import PositionsPanel from '../components/console/PositionsPanel'

const longPos: Position = {
  contract: 'BTC_USDT',
  size: 300,
  entry_price: 115_446.3,
  mark_price: 118_063,
  leverage: 5,
  margin: 708.38,
  unrealised_pnl: 78.5,
  liq_price: 105_842,
  stop_loss_price: 110_000,
  take_profit_price: 122_000,
}

const shortPos: Position = {
  contract: 'ETH_USDT',
  size: -30,
  entry_price: 3_780.3,
  mark_price: 3_598.2,
  leverage: 3,
  margin: 35.98,
  unrealised_pnl: -54.63,
  liq_price: 4_652,
  stop_loss_price: 3_900,
  take_profit_price: null,
}

describe('PositionsPanel(持仓卡片列表)', () => {
  it('空持仓显示空态', () => {
    render(<PositionsPanel positions={[]} />)
    expect(screen.getByText('当前无持仓')).toBeInTheDocument()
  })

  it('多头渲染：多 LONG 徽标 + 杠杆 + 价格字段 + 正盈亏绿色', () => {
    render(<PositionsPanel positions={[longPos]} />)
    expect(screen.getByText('BTC_USDT')).toBeInTheDocument()
    expect(screen.getByText('多 LONG')).toBeInTheDocument()
    expect(screen.getByText('5x')).toBeInTheDocument()
    expect(screen.getByText('+300')).toBeInTheDocument() // 张数正多带 + 号
    expect(screen.getByText('张数')).toBeInTheDocument()
    expect(screen.getByText('止损价')).toBeInTheDocument()
    expect(screen.getByText('止盈价')).toBeInTheDocument()
    expect(screen.getByText('开仓价')).toBeInTheDocument()
    expect(screen.getByText('115,446.30')).toBeInTheDocument()
    expect(screen.getByText('标记价')).toBeInTheDocument()
    expect(screen.getByText('强平价')).toBeInTheDocument()
    expect(screen.getByText('保证金')).toBeInTheDocument()
    expect(
      screen.queryByText(/entry_price|mark_price|liq_price|margin\(|stop_loss_price|take_profit_price/),
    ).not.toBeInTheDocument()
    expect(screen.getByText('708.38')).toBeInTheDocument()
    const pnl = screen.getByText('+78.50')
    expect(pnl.className).toContain('text-emerald-400')
    // 强平缓冲 = |118063 − 105842| / 118063 ≈ 10.35%；保证金收益率 = 78.5/708.38 ≈ +11.08%（正绿）
    expect(screen.getByText('10.35%')).toBeInTheDocument()
    const roi = screen.getByText('+11.08%')
    expect(roi.className).toContain('text-emerald-400')
  })

  it('空头渲染：空 SHORT 徽标 + 负盈亏红色', () => {
    render(<PositionsPanel positions={[shortPos]} />)
    expect(screen.getByText('ETH_USDT')).toBeInTheDocument()
    expect(screen.getByText('空 SHORT')).toBeInTheDocument()
    expect(screen.getByText('-30')).toBeInTheDocument()
    expect(screen.getByText('未设置')).toBeInTheDocument()
    const pnl = screen.getByText('-54.63')
    expect(pnl.className).toContain('text-rose-400')
    // 强平缓冲 = |3598.2 − 4652| / 3598.2 ≈ 29.29%；保证金收益率 = -54.63/35.98 ≈ -151.83%（负红）
    expect(screen.getByText('29.29%')).toBeInTheDocument()
    const roi = screen.getByText('-151.83%')
    expect(roi.className).toContain('text-rose-400')
  })

  it('无效值降级：mark/liq 为 0 强平缓冲显示「-」，margin ≤ 0 收益率显示「-」', () => {
    const broken: Position = { ...longPos, mark_price: 0, margin: 0 }
    render(<PositionsPanel positions={[broken]} />)
    // 强平缓冲与保证金收益率两个字段值均为 '-'
    const buffer = screen.getByText('强平缓冲').nextElementSibling!
    expect(buffer.textContent).toBe('-')
    const roi = screen.getByText('保证金收益率').nextElementSibling!
    expect(roi.textContent).toBe('-')
  })

  it('每张卡片都有手动平仓按钮', () => {
    render(<PositionsPanel positions={[longPos, shortPos]} />)
    expect(screen.getAllByRole('button', { name: '手动平仓' })).toHaveLength(2)
  })
})
