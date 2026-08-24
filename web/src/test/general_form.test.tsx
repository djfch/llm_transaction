/**
 * GeneralForm 渲染分支测试：
 * - 旧配置（llm 无 credentials）：provider/model/max_tokens/openai_base_url 四个字段照常渲染；
 * - 多凭证配置：凭证管理维护的四个字段隐藏，max_consecutive_failures 保留；
 * - 保存时提交体不携带 llm.credentials 与 agents，本表单不拥有这两段的写权。
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { AppConfig } from '../api/types'
import GeneralForm from '../pages/config/GeneralForm'

/** 旧版单凭证配置夹具（无 credentials） */
const LEGACY: AppConfig = {
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
    max_position_stop_risk_pct: 0.01,
    max_leverage: 5,
    daily_loss_limit: 0.1,
    max_orders_per_day: 20,
    max_deviation: 0.02,
    kill_switch: false,
  },
  scheduler: { default_wake_minutes: 60, min_wake_minutes: 5, max_wake_minutes: 720 },
  notify: { telegram_enabled: false },
}

/** 多凭证配置夹具 */
const WITH_CREDENTIALS: AppConfig = {
  ...LEGACY,
  llm: {
    ...LEGACY.llm,
    credentials: [
      {
        name: 'claude-main',
        provider: 'anthropic',
        model: 'claude-sonnet-4-5',
        max_tokens: 4096,
        openai_base_url: '',
        thinking_effort: '',
        api_key_env: 'LLM_KEY_CLAUDE_MAIN',
      },
    ],
  },
  agents: { trader: { credential: 'claude-main' }, reviewer: { credential: 'claude-main' } },
}

describe('GeneralForm(常规设置) · 渲染分支', () => {
  it('旧配置：llm 四个平铺字段照常渲染', () => {
    render(<GeneralForm initial={LEGACY} onSave={vi.fn()} />)

    expect(screen.getByLabelText('llm.provider')).toBeInTheDocument()
    expect(screen.getByLabelText('llm.model')).toBeInTheDocument()
    expect(screen.getByLabelText('llm.max_tokens')).toBeInTheDocument()
    expect(screen.getByLabelText('llm.openai_base_url')).toBeInTheDocument()
    expect(screen.getByLabelText('llm.max_consecutive_failures')).toBeInTheDocument()
  })

  it('多凭证配置：四个平铺字段隐藏，max_consecutive_failures 保留', () => {
    render(<GeneralForm initial={WITH_CREDENTIALS} onSave={vi.fn()} />)

    expect(screen.queryByLabelText('llm.provider')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('llm.model')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('llm.max_tokens')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('llm.openai_base_url')).not.toBeInTheDocument()
    expect(screen.getByLabelText('llm.max_consecutive_failures')).toBeInTheDocument()
    expect(screen.getByText(/已迁入「密钥状态」小节的凭证管理/)).toBeInTheDocument()
  })

  it('保存：提交体不携带 llm.credentials 与 agents（未提及的段保留服务端现值）', async () => {
    const onSave = vi.fn().mockResolvedValue(undefined)
    render(<GeneralForm initial={WITH_CREDENTIALS} onSave={onSave} />)

    fireEvent.click(screen.getByRole('button', { name: '保存常规设置' }))
    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1))
    const sent = onSave.mock.calls[0][0] as AppConfig
    // 本表单不编辑这两段，因此提交体不得携带对应快照
    expect('credentials' in sent.llm).toBe(false)
    expect('agents' in sent).toBe(false)
    // 本表单负责的字段照常提交
    expect(sent.llm.provider).toBe('anthropic')
    expect(sent.llm.max_consecutive_failures).toBe(3)
    expect(sent.risk).toEqual(WITH_CREDENTIALS.risk)
  })
})
