/**
 * Agent 凭证分配测试：下拉选项来自凭证列表（旧配置仅 default）、
 * 初值回显 agents 段、保存经 onSave 提交写入 agents 段的完整配置。
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { AppConfig } from '../api/types'
import AgentCredentialsForm from '../pages/config/AgentCredentialsForm'

/** 配置夹具：agents 段已分配（决策 claude-main / 复盘 deepseek-backup） */
const CONFIG: AppConfig = {
  mode: 'paper',
  llm: {
    provider: 'anthropic',
    model: 'claude-sonnet-4-5',
    max_tokens: 4096,
    openai_base_url: '',
    max_consecutive_failures: 3,
    credentials: [
      {
        name: 'claude-main',
        provider: 'anthropic',
        model: 'claude-sonnet-4-5',
        max_tokens: 4096,
        openai_base_url: '',
        api_key_env: 'LLM_KEY_CLAUDE_MAIN',
      },
      {
        name: 'deepseek-backup',
        provider: 'openai_compat',
        model: 'deepseek-chat',
        max_tokens: 4096,
        openai_base_url: 'https://api.deepseek.com/v1',
        api_key_env: 'LLM_KEY_DEEPSEEK_BACKUP',
      },
    ],
  },
  agents: { trader: { credential: 'claude-main' }, reviewer: { credential: 'deepseek-backup' } },
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

const NAMES = ['claude-main', 'deepseek-backup']

describe('AgentCredentialsForm(凭证分配)', () => {
  it('两个下拉渲染凭证选项并回显 agents 段初值', () => {
    render(<AgentCredentialsForm initial={CONFIG} credentialNames={NAMES} onSave={vi.fn()} />)

    const trader = screen.getByLabelText('agents.trader.credential') as HTMLSelectElement
    const reviewer = screen.getByLabelText('agents.reviewer.credential') as HTMLSelectElement
    expect(trader.value).toBe('claude-main')
    expect(reviewer.value).toBe('deepseek-backup')
    for (const select of [trader, reviewer]) {
      expect(withinOptions(select)).toEqual(NAMES)
    }
    // 中文释义提示
    expect(screen.getByText(/决策 agent/)).toBeInTheDocument()
    expect(screen.getByText(/复盘 agent/)).toBeInTheDocument()
  })

  it('无 credentials 的旧配置：选项仅 default，缺省选中 default', () => {
    const legacy: AppConfig = { ...CONFIG, agents: undefined, llm: { ...CONFIG.llm, credentials: undefined } }
    render(<AgentCredentialsForm initial={legacy} credentialNames={[]} onSave={vi.fn()} />)

    const trader = screen.getByLabelText('agents.trader.credential') as HTMLSelectElement
    expect(withinOptions(trader)).toEqual(['default'])
    expect(trader.value).toBe('default')
  })

  it('保存：onSave 收到写入 agents 段的完整配置（其余段原样透传）', async () => {
    const onSave = vi.fn().mockResolvedValue(undefined)
    render(<AgentCredentialsForm initial={CONFIG} credentialNames={NAMES} onSave={onSave} />)

    fireEvent.change(screen.getByLabelText('agents.reviewer.credential'), {
      target: { value: 'claude-main' },
    })
    fireEvent.click(screen.getByRole('button', { name: '保存凭证分配' }))

    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1))
    const sent = onSave.mock.calls[0][0] as AppConfig
    expect(sent.agents).toEqual({
      trader: { credential: 'claude-main' },
      reviewer: { credential: 'claude-main' },
    })
    expect(sent.llm).toEqual(CONFIG.llm)
    expect(sent.risk).toEqual(CONFIG.risk)
    expect(await screen.findByText(/^已保存 /)).toBeInTheDocument()
  })

  it('保存失败展示错误且不显示已保存标记', async () => {
    const onSave = vi.fn().mockRejectedValue(new Error('422: 引用的凭证不存在'))
    render(<AgentCredentialsForm initial={CONFIG} credentialNames={NAMES} onSave={onSave} />)

    fireEvent.click(screen.getByRole('button', { name: '保存凭证分配' }))
    expect(await screen.findByText(/422: 引用的凭证不存在/)).toBeInTheDocument()
    expect(screen.queryByText(/^已保存 /)).not.toBeInTheDocument()
  })
})

/** 读取 select 的 option 值列表（有序断言用） */
function withinOptions(select: HTMLSelectElement): string[] {
  return Array.from(select.options).map((o) => o.value)
}
