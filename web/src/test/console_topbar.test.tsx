/**
 * 顶部状态栏测试：status 渲染（产品名/mode 徽标/LLM 状态）、
 * llm_configured=false 琥珀横幅、WS 连接指示灯、配置入口回调。
 */
import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { StatusInfo } from '../api/types'
import TopBar from '../components/console/TopBar'

const baseStatus: StatusInfo = {
  mode: 'paper',
  uptime_seconds: 3600,
  kill_switch: false,
  llm_provider: 'anthropic',
  llm_model: 'claude-sonnet-4-5',
  llm_configured: true,
  agent_running: true,
}

function renderBar(overrides: Partial<StatusInfo> | null = {}, wsConnected = true) {
  const onOpenConfig = vi.fn()
  render(
    <TopBar
      status={overrides === null ? null : { ...baseStatus, ...overrides }}
      wsConnected={wsConnected}
      onOpenConfig={onOpenConfig}
    />,
  )
  return { onOpenConfig }
}

describe('TopBar(顶部状态栏) status 渲染', () => {
  it('渲染产品名与 paper 徽标（琥珀）', () => {
    renderBar()
    expect(screen.getByText(/LLM 交易/)).toBeInTheDocument()
    const badge = screen.getByText('模拟盘')
    expect(badge).toBeInTheDocument()
    expect(badge.className).toContain('text-amber-300')
  })

  it('testnet 徽标为青色', () => {
    renderBar({ mode: 'testnet' })
    const badge = screen.getByText('沙盒')
    expect(badge.className).toContain('text-cyan-300')
  })

  it('显示 LLM provider/model 与已配置', () => {
    renderBar()
    expect(screen.getByText('anthropic · claude-sonnet-4-5')).toBeInTheDocument()
    expect(screen.getByText('已配置')).toBeInTheDocument()
  })

  it('status=null 时 agent/kill 按钮禁用且显示占位', () => {
    renderBar(null)
    expect(screen.getByRole('button', { name: '启动' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '⏻ 紧急熔断' })).toBeDisabled()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })
})

describe('TopBar llm_configured=false 琥珀横幅', () => {
  it('未配置：渲染横幅(role=alert)与未配置标识', () => {
    renderBar({ llm_configured: false })
    expect(screen.getByRole('alert')).toHaveTextContent('自动决策已暂停')
    expect(screen.getAllByText('未配置').length).toBeGreaterThan(0)
  })

  it('已配置：不渲染横幅', () => {
    renderBar({ llm_configured: true })
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })
})

describe('TopBar WS 指示灯与配置入口', () => {
  it('wsConnected=true 显示行情已连接', () => {
    renderBar({}, true)
    expect(screen.getByText('行情已连接')).toBeInTheDocument()
  })

  it('wsConnected=false 显示行情未连接', () => {
    renderBar({}, false)
    expect(screen.getByText('行情未连接')).toBeInTheDocument()
  })

  it('点击齿轮按钮触发 onOpenConfig', () => {
    const { onOpenConfig } = renderBar()
    fireEvent.click(screen.getByRole('button', { name: '打开配置中心' }))
    expect(onOpenConfig).toHaveBeenCalledTimes(1)
  })

  it('未配置横幅中的前往配置按钮也触发 onOpenConfig', () => {
    const { onOpenConfig } = renderBar({ llm_configured: false })
    fireEvent.click(screen.getByRole('button', { name: '前往配置 LLM API 密钥' }))
    expect(onOpenConfig).toHaveBeenCalledTimes(1)
  })
})
