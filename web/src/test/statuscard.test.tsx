/**
 * 运行状态卡测试：llm_configured(LLM配置) 徽标的已配置 / 未配置展示。
 */
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { StatusInfo } from '../api/types'
import StatusCard from '../components/StatusCard'

const baseStatus: StatusInfo = {
  mode: 'paper',
  uptime_seconds: 3600,
  kill_switch: false,
  llm_provider: 'anthropic',
  llm_model: 'claude-sonnet-4-5',
  llm_configured: true,
  agent_running: true,
}

function renderCard(status: StatusInfo) {
  return render(<StatusCard status={status} loading={false} error={null} onChanged={() => {}} />)
}

describe('StatusCard(llm_configured 徽标)', () => {
  it('llm_configured=true 显示"已配置"', () => {
    renderCard(baseStatus)
    expect(screen.getByText('llm_configured(LLM配置)')).toBeInTheDocument()
    expect(screen.getByText('已配置')).toBeInTheDocument()
  })

  it('llm_configured=false 显示"未配置"', () => {
    renderCard({ ...baseStatus, llm_configured: false })
    expect(screen.getByText('llm_configured(LLM配置)')).toBeInTheDocument()
    expect(screen.getByText('未配置')).toBeInTheDocument()
  })
})
