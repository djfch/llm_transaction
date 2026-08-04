/**
 * StrategyIndicatorsBar 渲染测试：短名单名称行（· 连接）、scalar 徽标值、错误态与空态。
 */
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import StrategyIndicatorsBar from '../components/console/StrategyIndicatorsBar'

const NAMES = ['EMA20', 'EMA50', 'RSI14', 'MACD', 'ATR14', '持仓量']
const BADGES = [
  { key: 'atr14', label: 'ATR14', text: '892.54' },
  { key: 'oi', label: '持仓量', text: '123,456' },
]

describe('StrategyIndicatorsBar', () => {
  it('显示短名单名称行与 scalar 徽标值', () => {
    render(<StrategyIndicatorsBar names={NAMES} badges={BADGES} error={null} />)
    const bar = screen.getByTestId('strategy-indicators-bar')
    expect(bar.textContent).toContain('当前策略指标：EMA20 · EMA50 · RSI14 · MACD · ATR14 · 持仓量')
    expect(bar.textContent).toContain('ATR14 892.54')
    expect(bar.textContent).toContain('持仓量 123,456')
  })

  it('scalar 无数据时显示占位文案', () => {
    render(
      <StrategyIndicatorsBar names={NAMES} badges={[{ key: 'oi', label: '持仓量', text: '无数据' }]} error={null} />,
    )
    expect(screen.getByTestId('strategy-indicators-bar').textContent).toContain('持仓量 无数据')
  })

  it('加载失败显示错误文本', () => {
    render(<StrategyIndicatorsBar names={[]} badges={[]} error="网络错误" />)
    expect(screen.getByTestId('strategy-indicators-bar').textContent).toContain('指标加载失败：网络错误')
  })

  it('短名单为空（且无错误）时不渲染', () => {
    render(<StrategyIndicatorsBar names={[]} badges={[]} error={null} />)
    expect(screen.queryByTestId('strategy-indicators-bar')).not.toBeInTheDocument()
  })
})
