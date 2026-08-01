/**
 * LLM 凭证管理测试：凭证列表渲染（名称/厂商/模型/env/key 状态/used_by 中文徽标）、
 * 行内保存 key 走 credential/api_key 契约、新增凭证追加提交 llm.credentials 全量、
 * 删除（used_by 非空禁用 + confirm 确认）、旧配置兼容渲染（引导提示 + 旧表单）。
 * 回归覆盖：
 * - B1：旧配置（config 无 llm.credentials）新增首条凭证时，提交体须物化 default 凭证
 *   （api_key_env 推导与后端 resolve_credentials 一致），否则后端校验 agents 引用必然 422；
 * - M4：新增/删除基于 api.getConfig() 的服务器最新态做 read-modify-write，不丢他处变更。
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { AppConfig, SecretsStatus } from '../api/types'
import SecretsForm from '../pages/config/SecretsForm'

// 隔离 API 层：凭证管理依赖 setSecrets（保存 key）、getConfig/putConfig（新增/删除凭证）
const { setSecrets, putConfig, getConfig } = vi.hoisted(() => ({
  setSecrets: vi.fn(),
  putConfig: vi.fn(),
  getConfig: vi.fn(),
}))
vi.mock('../api', () => ({ api: { setSecrets, putConfig, getConfig } }))

/** 多凭证状态夹具：claude-main 已配置且被决策引用；deepseek-backup 未配置、未被引用 */
const STATUS: SecretsStatus = {
  gate_key: true,
  llm_key: true,
  telegram: false,
  credentials: [
    {
      name: 'claude-main',
      provider: 'anthropic',
      model: 'claude-sonnet-4-5',
      api_key_env: 'LLM_KEY_CLAUDE_MAIN',
      key_configured: true,
      used_by: ['trader'],
    },
    {
      name: 'deepseek-backup',
      provider: 'openai_compat',
      model: 'deepseek-chat',
      api_key_env: 'LLM_KEY_DEEPSEEK_BACKUP',
      key_configured: false,
      used_by: [],
    },
  ],
}

/** 与 STATUS 对应的配置夹具（PUT /api/config 提交载体） */
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
  agents: { trader: { credential: 'claude-main' }, reviewer: { credential: 'default' } },
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

/** 按凭证名取所在卡片作用域（卡片以名称文本定位，行内控件在 li 内） */
function rowOf(name: string): HTMLElement {
  return screen.getByText(name).closest('li') as HTMLElement
}

beforeEach(() => {
  setSecrets.mockReset()
  putConfig.mockReset()
  // 默认：服务器最新态与 config prop 一致（个别用例用 mockResolvedValue 覆盖模拟他处变更）
  getConfig.mockReset().mockResolvedValue(CONFIG)
})

afterEach(() => vi.unstubAllGlobals())

describe('SecretsForm(凭证管理) · 列表与保存 key', () => {
  it('渲染两条凭证卡片：名称/provider/model/env/key 状态与 used_by 中文徽标', () => {
    render(<SecretsForm status={STATUS} config={CONFIG} onSaved={() => {}} />)

    const main = rowOf('claude-main')
    expect(within(main).getByText(/anthropic · claude-sonnet-4-5 · LLM_KEY_CLAUDE_MAIN/)).toBeInTheDocument()
    expect(within(main).getByText('决策')).toBeInTheDocument() // trader 徽标只保留中文释义
    expect(within(main).getByText('已配置')).toBeInTheDocument()

    const backup = rowOf('deepseek-backup')
    expect(within(backup).getByText(/openai_compat · deepseek-chat · LLM_KEY_DEEPSEEK_BACKUP/)).toBeInTheDocument()
    expect(within(backup).getByText('未被引用')).toBeInTheDocument()
    expect(within(backup).getByText('未配置')).toBeInTheDocument()

    // 只读状态区保留；旧表单不再渲染
    expect(screen.getByText('gate_key')).toBeInTheDocument()
    expect(screen.queryByLabelText('ANTHROPIC_API_KEY')).not.toBeInTheDocument()
  })

  it('行内保存 key：走 POST /api/secrets 的 credential/api_key 形式，成功清空输入并回调', async () => {
    setSecrets.mockResolvedValue({ saved: true, llm_configured: true, error: '' })
    const onSaved = vi.fn()
    render(<SecretsForm status={STATUS} config={CONFIG} onSaved={onSaved} />)

    const main = rowOf('claude-main')
    const input = within(main).getByLabelText('claude-main 的 API Key')
    expect(input).toHaveAttribute('type', 'password')
    fireEvent.change(input, { target: { value: 'sk-ant-new' } })
    fireEvent.click(within(main).getByRole('button', { name: '保存 key' }))

    await waitFor(() => expect(setSecrets).toHaveBeenCalledWith({ credential: 'claude-main', api_key: 'sk-ant-new' }))
    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1))
    expect(input).toHaveValue('')
    expect(putConfig).not.toHaveBeenCalled()
  })

  it('保存 key 接口报错（error 非空）时不清空输入、不回调 onSaved', async () => {
    setSecrets.mockResolvedValue({ saved: true, llm_configured: true, error: 'provider 重建失败' })
    const onSaved = vi.fn()
    render(<SecretsForm status={STATUS} config={CONFIG} onSaved={onSaved} />)

    const main = rowOf('claude-main')
    const input = within(main).getByLabelText('claude-main 的 API Key')
    fireEvent.change(input, { target: { value: 'sk-ant-new' } })
    fireEvent.click(within(main).getByRole('button', { name: '保存 key' }))

    const alert = await within(main).findByRole('alert')
    expect(alert).toHaveTextContent('provider 重建失败')
    expect(input).toHaveValue('sk-ant-new')
    expect(onSaved).not.toHaveBeenCalled()
  })
})

describe('SecretsForm(凭证管理) · 新增凭证', () => {
  it('填写表单后保存：PUT /api/config 提交追加后的 llm.credentials 全量列表', async () => {
    putConfig.mockResolvedValue({ saved: true, needs_restart: [], llm_configured: true, llm_error: '' })
    const onSaved = vi.fn()
    render(<SecretsForm status={STATUS} config={CONFIG} onSaved={onSaved} />)

    fireEvent.change(screen.getByLabelText('name'), { target: { value: 'kimi-bak' } })
    fireEvent.change(screen.getByLabelText('provider'), { target: { value: 'openai_compat' } })
    // 非 anthropic（openai_compat / openai_responses）时才出现 base_url 输入框
    const baseUrl = screen.getByLabelText('openai_base_url')
    fireEvent.change(screen.getByLabelText('model'), { target: { value: 'kimi-k2' } })
    fireEvent.change(baseUrl, { target: { value: 'https://api.moonshot.cn/v1' } })
    fireEvent.click(screen.getByRole('button', { name: '保存新凭证' }))

    await waitFor(() => expect(putConfig).toHaveBeenCalledTimes(1))
    const sent = putConfig.mock.calls[0][0] as AppConfig
    expect(sent.llm.credentials).toHaveLength(3)
    expect(sent.llm.credentials?.[2]).toEqual({
      name: 'kimi-bak',
      provider: 'openai_compat',
      model: 'kimi-k2',
      max_tokens: 4096,
      openai_base_url: 'https://api.moonshot.cn/v1',
      api_key_env: 'LLM_KEY_KIMI_BAK',
    })
    // 其余配置段原样透传
    expect(sent.risk).toEqual(CONFIG.risk)
    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1))
  })

  it('openai_responses 同样显示 base_url 输入框，且可留空提交（走 OpenAI 官方端点）', async () => {
    putConfig.mockResolvedValue({ saved: true, needs_restart: [], llm_configured: true, llm_error: '' })
    render(<SecretsForm status={STATUS} config={CONFIG} onSaved={() => {}} />)

    // anthropic 不显示 base_url 输入框
    expect(screen.queryByLabelText('openai_base_url')).not.toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('name'), { target: { value: 'gpt-main' } })
    fireEvent.change(screen.getByLabelText('provider'), { target: { value: 'openai_responses' } })
    // openai_responses 出现 base_url 输入框，但允许留空
    expect(screen.getByLabelText('openai_base_url')).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('model'), { target: { value: 'gpt-5' } })
    fireEvent.click(screen.getByRole('button', { name: '保存新凭证' }))

    await waitFor(() => expect(putConfig).toHaveBeenCalledTimes(1))
    const sent = putConfig.mock.calls[0][0] as AppConfig
    expect(sent.llm.credentials?.[2]).toEqual({
      name: 'gpt-main',
      provider: 'openai_responses',
      model: 'gpt-5',
      max_tokens: 4096,
      openai_base_url: '',
      api_key_env: 'LLM_KEY_GPT_MAIN',
    })
  })

  it('非法名称（大写/空格）被前端拦截，不发起请求', async () => {
    render(<SecretsForm status={STATUS} config={CONFIG} onSaved={() => {}} />)

    fireEvent.change(screen.getByLabelText('name'), { target: { value: 'Bad Name' } })
    fireEvent.change(screen.getByLabelText('model'), { target: { value: 'x' } })
    fireEvent.click(screen.getByRole('button', { name: '保存新凭证' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('名称仅允许小写字母、数字与连字符')
    expect(putConfig).not.toHaveBeenCalled()
  })

  it('回归 M4：保存期间服务器侧已新增他处凭证时，提交列表基于 getConfig 最新态不丢它', async () => {
    // 服务器最新态比他处旧快照多一条 other-added（如另一标签页刚新增）
    const extra = {
      name: 'other-added',
      provider: 'anthropic' as const,
      model: 'claude-haiku-4-5',
      max_tokens: 2048,
      openai_base_url: '',
      api_key_env: 'LLM_KEY_OTHER_ADDED',
    }
    getConfig.mockResolvedValue({
      ...CONFIG,
      llm: { ...CONFIG.llm, credentials: [...(CONFIG.llm.credentials ?? []), extra] },
    })
    putConfig.mockResolvedValue({ saved: true, needs_restart: [], llm_configured: true, llm_error: '' })
    render(<SecretsForm status={STATUS} config={CONFIG} onSaved={() => {}} />)

    fireEvent.change(screen.getByLabelText('name'), { target: { value: 'kimi-bak' } })
    fireEvent.change(screen.getByLabelText('model'), { target: { value: 'kimi-k2' } })
    fireEvent.click(screen.getByRole('button', { name: '保存新凭证' }))

    await waitFor(() => expect(putConfig).toHaveBeenCalledTimes(1))
    const sent = putConfig.mock.calls[0][0] as AppConfig
    expect(sent.llm.credentials?.map((c) => c.name)).toEqual([
      'claude-main',
      'deepseek-backup',
      'other-added',
      'kimi-bak',
    ])
  })
})

describe('SecretsForm(凭证管理) · 旧配置迁移（回归 B1）', () => {
  /** 旧配置状态夹具：后端 secrets/status 合成 default 凭证，但 config 无 llm.credentials 段 */
  const LEGACY_STATUS: SecretsStatus = {
    gate_key: true,
    llm_key: true,
    telegram: false,
    credentials: [
      {
        name: 'default',
        provider: 'anthropic',
        model: 'claude-sonnet-4-5',
        api_key_env: 'ANTHROPIC_API_KEY',
        key_configured: true,
        used_by: ['trader', 'reviewer'],
      },
    ],
  }

  const LEGACY_CONFIG: AppConfig = {
    ...CONFIG,
    llm: {
      provider: 'anthropic',
      model: 'claude-sonnet-4-5',
      max_tokens: 4096,
      openai_base_url: '',
      max_consecutive_failures: 3,
      // 无 credentials 段：旧版单凭证配置
    },
    agents: undefined,
  }

  /** 填名称与模型并保存新凭证，返回提交体 */
  async function saveNewCredential(): Promise<AppConfig> {
    fireEvent.change(screen.getByLabelText('name'), { target: { value: 'kimi-bak' } })
    fireEvent.change(screen.getByLabelText('model'), { target: { value: 'kimi-k2' } })
    fireEvent.click(screen.getByRole('button', { name: '保存新凭证' }))
    await waitFor(() => expect(putConfig).toHaveBeenCalledTimes(1))
    return putConfig.mock.calls[0][0] as AppConfig
  }

  it('旧配置（anthropic）新增首条凭证：提交体物化 default 凭证 + 新凭证，api_key_env=ANTHROPIC_API_KEY', async () => {
    getConfig.mockResolvedValue(LEGACY_CONFIG)
    putConfig.mockResolvedValue({ saved: true, needs_restart: [], llm_configured: true, llm_error: '' })
    render(<SecretsForm status={LEGACY_STATUS} config={LEGACY_CONFIG} onSaved={() => {}} />)

    const sent = await saveNewCredential()
    // 与后端 resolve_credentials 逐字段对齐：缺 default 会被 agents 缺省引用校验 422
    expect(sent.llm.credentials?.[0]).toEqual({
      name: 'default',
      provider: 'anthropic',
      model: 'claude-sonnet-4-5',
      max_tokens: 4096,
      openai_base_url: '',
      api_key_env: 'ANTHROPIC_API_KEY',
    })
    expect(sent.llm.credentials?.[1].name).toBe('kimi-bak')
    expect(sent.llm.credentials).toHaveLength(2)
  })

  it('旧配置（openai_compat）新增首条凭证：物化 default 的 api_key_env=OPENAI_API_KEY 且携带 base_url', async () => {
    const legacyOpenai: AppConfig = {
      ...LEGACY_CONFIG,
      llm: {
        ...LEGACY_CONFIG.llm,
        provider: 'openai_compat',
        model: 'deepseek-chat',
        openai_base_url: 'https://api.deepseek.com/v1',
      },
    }
    getConfig.mockResolvedValue(legacyOpenai)
    putConfig.mockResolvedValue({ saved: true, needs_restart: [], llm_configured: true, llm_error: '' })
    render(<SecretsForm status={LEGACY_STATUS} config={legacyOpenai} onSaved={() => {}} />)

    const sent = await saveNewCredential()
    expect(sent.llm.credentials?.[0]).toEqual({
      name: 'default',
      provider: 'openai_compat',
      model: 'deepseek-chat',
      max_tokens: 4096,
      openai_base_url: 'https://api.deepseek.com/v1',
      api_key_env: 'OPENAI_API_KEY',
    })
    expect(sent.llm.credentials?.[1].name).toBe('kimi-bak')
  })
})

describe('SecretsForm(凭证管理) · 删除凭证', () => {
  it('未被引用的凭证可删除：confirm 后 PUT 移除该条的全量列表', async () => {
    vi.stubGlobal('confirm', vi.fn().mockReturnValue(true))
    putConfig.mockResolvedValue({ saved: true, needs_restart: [], llm_configured: true, llm_error: '' })
    const onSaved = vi.fn()
    render(<SecretsForm status={STATUS} config={CONFIG} onSaved={onSaved} />)

    const backup = rowOf('deepseek-backup')
    const deleteBtn = within(backup).getByRole('button', { name: '删除' })
    expect(deleteBtn).toBeEnabled()
    fireEvent.click(deleteBtn)

    expect(window.confirm).toHaveBeenCalledWith('确认删除凭证「deepseek-backup」？')
    await waitFor(() => expect(putConfig).toHaveBeenCalledTimes(1))
    const sent = putConfig.mock.calls[0][0] as AppConfig
    expect(sent.llm.credentials?.map((c) => c.name)).toEqual(['claude-main'])
    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1))
  })

  it('仍被 agent 引用的凭证删除按钮禁用', () => {
    render(<SecretsForm status={STATUS} config={CONFIG} onSaved={() => {}} />)
    const main = rowOf('claude-main')
    expect(within(main).getByRole('button', { name: '删除' })).toBeDisabled()
  })

  it('confirm 取消时不发起请求', () => {
    vi.stubGlobal('confirm', vi.fn().mockReturnValue(false))
    render(<SecretsForm status={STATUS} config={CONFIG} onSaved={() => {}} />)
    fireEvent.click(within(rowOf('deepseek-backup')).getByRole('button', { name: '删除' }))
    expect(putConfig).not.toHaveBeenCalled()
  })
})

describe('SecretsForm(凭证管理) · 旧配置兼容', () => {
  it('credentials 为空数组：显示 default 引导提示并保留旧两输入框表单', () => {
    const legacy: SecretsStatus = { gate_key: true, llm_key: true, telegram: false, credentials: [] }
    render(<SecretsForm status={legacy} config={null} onSaved={() => {}} />)

    expect(screen.getByText(/旧版单凭证（default）配置/)).toBeInTheDocument()
    expect(screen.getByLabelText('ANTHROPIC_API_KEY')).toBeInTheDocument()
    expect(screen.getByLabelText('OPENAI_API_KEY')).toBeInTheDocument()
    expect(screen.queryByText('新增凭证')).not.toBeInTheDocument()
  })
})
