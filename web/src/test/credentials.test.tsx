/**
 * LLM 凭证管理测试（POST/PUT/DELETE /api/credentials 专用端点）：
 * - 列表渲染：名称 / provider·model·env 文本 / used_by 中文徽标 / key 状态；
 * - create：统一表单一次提交"定义 + key"（提交体逐字核对），名称非法与重名前端拦截
 *   （不发请求），成功清空表单，llm_error 非空显示琥珀警告条；
 * - edit：卡片内联展开，初值回显（config.llm.credentials 按 name 取；default 合成凭证回退
 *   平铺 llm 字段），name 锁定无输入框，api_key 留空时提交体不含该键，保存成功收起；
 * - delete：confirm 确认后调 deleteCredential(name)，used_by 非空禁用；
 * - 旧配置分支：credentials 为空时渲染旧两输入框表单，不显示新增表单。
 * 服务端专用端点协调凭证变更，前端不物化 default 凭证，也不执行 read-modify-write。
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { AppConfig, SecretsStatus } from '../api/types'
import SecretsForm from '../pages/config/SecretsForm'

// 隔离 API 层：凭证管理只依赖专用端点三方法（旧两输入框分支不在本文件覆盖）
const { createCredential, updateCredential, deleteCredential } = vi.hoisted(() => ({
  createCredential: vi.fn(),
  updateCredential: vi.fn(),
  deleteCredential: vi.fn(),
}))
vi.mock('../api', () => ({ api: { createCredential, updateCredential, deleteCredential } }))

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

/** 与 STATUS 对应的配置夹具（编辑初值来源） */
const CONFIG: AppConfig = {
  mode: 'paper',
  llm: {
    provider: 'anthropic',
    model: 'claude-sonnet-4-5',
    max_tokens: 4096,
    openai_base_url: '',
    thinking_effort: '',
    max_consecutive_failures: 3,
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
      {
        name: 'deepseek-backup',
        provider: 'openai_compat',
        model: 'deepseek-chat',
        max_tokens: 8192,
        openai_base_url: 'https://api.deepseek.com/v1',
        thinking_effort: 'high',
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

/** 增/改/删统一成功响应 */
const OK = { saved: true, key_saved: true, llm_configured: true, llm_error: '' }

/** 按凭证名取所在卡片作用域（卡片以名称文本定位，行内控件在 li 内） */
function rowOf(name: string): HTMLElement {
  return screen.getByText(name).closest('li') as HTMLElement
}

beforeEach(() => {
  createCredential.mockReset()
  updateCredential.mockReset()
  deleteCredential.mockReset()
})

afterEach(() => vi.unstubAllGlobals())

describe('SecretsForm(凭证管理) · 列表渲染', () => {
  it('渲染两条凭证卡片：名称/provider·model·env/key 状态与 used_by 中文徽标', () => {
    render(<SecretsForm status={STATUS} config={CONFIG} onSaved={() => {}} />)

    const main = rowOf('claude-main')
    expect(within(main).getByText(/anthropic · claude-sonnet-4-5 · LLM_KEY_CLAUDE_MAIN/)).toBeInTheDocument()
    expect(within(main).getByText('决策')).toBeInTheDocument() // trader 徽标只保留中文释义
    expect(within(main).getByText('已配置')).toBeInTheDocument()

    const backup = rowOf('deepseek-backup')
    expect(within(backup).getByText(/openai_compat · deepseek-chat · LLM_KEY_DEEPSEEK_BACKUP/)).toBeInTheDocument()
    expect(within(backup).getByText('未被引用')).toBeInTheDocument()
    expect(within(backup).getByText('未配置')).toBeInTheDocument()

    // 只读状态区保留；旧两输入框表单不再渲染
    expect(screen.getByText('gate_key')).toBeInTheDocument()
    expect(screen.queryByLabelText('ANTHROPIC_API_KEY')).not.toBeInTheDocument()
  })
})

describe('SecretsForm(凭证管理) · 新增凭证', () => {
  /** 填齐新增表单（openai_compat 含 base_url 与 api_key）并提交 */
  function fillAndSave(apiKey: string) {
    fireEvent.change(screen.getByLabelText('name'), { target: { value: 'kimi-bak' } })
    fireEvent.change(screen.getByLabelText('provider'), { target: { value: 'openai_compat' } })
    fireEvent.change(screen.getByLabelText('model'), { target: { value: 'kimi-k2' } })
    fireEvent.change(screen.getByLabelText('openai_base_url'), {
      target: { value: 'https://api.moonshot.cn/v1' },
    })
    fireEvent.change(screen.getByLabelText('api_key'), { target: { value: apiKey } })
    fireEvent.click(screen.getByRole('button', { name: '保存新凭证' }))
  }

  it('填齐字段（含 api_key）提交：createCredential 契约逐字核对，成功后清空表单并回调', async () => {
    createCredential.mockResolvedValue(OK)
    const onSaved = vi.fn()
    render(<SecretsForm status={STATUS} config={CONFIG} onSaved={onSaved} />)

    expect(screen.getByLabelText('api_key')).toHaveAttribute('placeholder', '可留空，稍后在编辑中补')
    fillAndSave('sk-test')

    await waitFor(() =>
      expect(createCredential).toHaveBeenCalledWith({
        name: 'kimi-bak',
        provider: 'openai_compat',
        model: 'kimi-k2',
        max_tokens: 4096,
        openai_base_url: 'https://api.moonshot.cn/v1',
        thinking_effort: '',
        api_key: 'sk-test',
      }),
    )
    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1))
    // 成功清空表单字段
    expect(screen.getByLabelText('name')).toHaveValue('')
    expect(screen.getByLabelText('model')).toHaveValue('')
    expect(screen.getByLabelText('openai_base_url')).toHaveValue('')
    expect(screen.getByLabelText('api_key')).toHaveValue('')
    expect(screen.getByText('已保存')).toBeInTheDocument()
  })

  it('选择思考程度下拉：提交体带对应档位值', async () => {
    createCredential.mockResolvedValue(OK)
    render(<SecretsForm status={STATUS} config={CONFIG} onSaved={() => {}} />)

    fireEvent.change(screen.getByLabelText('name'), { target: { value: 'deepseek-main' } })
    fireEvent.change(screen.getByLabelText('model'), { target: { value: 'deepseek-v4-pro' } })
    fireEvent.change(screen.getByLabelText('思考程度'), { target: { value: 'high' } })
    fireEvent.click(screen.getByRole('button', { name: '保存新凭证' }))

    await waitFor(() =>
      expect(createCredential).toHaveBeenCalledWith(
        expect.objectContaining({
          model: 'deepseek-v4-pro',
          thinking_effort: 'high',
        }),
      ),
    )
  })

  it('api_key 为纯空白：提交体不含 api_key 键（等价留空，不写入 .env）', async () => {
    createCredential.mockResolvedValue(OK)
    render(<SecretsForm status={STATUS} config={CONFIG} onSaved={() => {}} />)

    fillAndSave('   ')

    await waitFor(() => expect(createCredential).toHaveBeenCalledTimes(1))
    expect(createCredential.mock.calls[0][0]).not.toHaveProperty('api_key')
  })

  it('api_key 带首尾空白：提交 trim 后的值', async () => {
    createCredential.mockResolvedValue(OK)
    render(<SecretsForm status={STATUS} config={CONFIG} onSaved={() => {}} />)

    fillAndSave('  sk-padded  ')

    await waitFor(() =>
      expect(createCredential).toHaveBeenCalledWith(
        expect.objectContaining({ api_key: 'sk-padded' }),
      ),
    )
  })

  it('非法名称（大写/空格）被前端拦截，不发起请求', async () => {
    render(<SecretsForm status={STATUS} config={CONFIG} onSaved={() => {}} />)

    fireEvent.change(screen.getByLabelText('name'), { target: { value: 'Bad Name' } })
    fireEvent.change(screen.getByLabelText('model'), { target: { value: 'x' } })
    fireEvent.click(screen.getByRole('button', { name: '保存新凭证' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('名称仅允许小写字母、数字与连字符')
    expect(createCredential).not.toHaveBeenCalled()
  })

  it('与现有凭证重名被前端拦截，不发起请求', async () => {
    render(<SecretsForm status={STATUS} config={CONFIG} onSaved={() => {}} />)

    fireEvent.change(screen.getByLabelText('name'), { target: { value: 'claude-main' } })
    fireEvent.change(screen.getByLabelText('model'), { target: { value: 'x' } })
    fireEvent.click(screen.getByRole('button', { name: '保存新凭证' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('凭证「claude-main」已存在')
    expect(createCredential).not.toHaveBeenCalled()
  })

  it('llm_error 非空（已保存但热重建失败）显示琥珀警告条', async () => {
    createCredential.mockResolvedValue({ ...OK, llm_error: 'provider 重建失败' })
    render(<SecretsForm status={STATUS} config={CONFIG} onSaved={() => {}} />)

    fillAndSave('')

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('provider 重建失败')
    expect(alert).toHaveClass('text-amber-300')
  })
})

describe('SecretsForm(凭证管理) · 编辑凭证', () => {
  it('点「编辑」内联展开：初值从 config.llm.credentials 回显，name 锁定无输入框', () => {
    render(<SecretsForm status={STATUS} config={CONFIG} onSaved={() => {}} />)

    const backup = rowOf('deepseek-backup')
    fireEvent.click(within(backup).getByRole('button', { name: '编辑' }))

    expect(within(backup).getByLabelText('provider')).toHaveValue('openai_compat')
    expect(within(backup).getByLabelText('model')).toHaveValue('deepseek-chat')
    expect(within(backup).getByLabelText('max_tokens')).toHaveValue('8192')
    expect(within(backup).getByLabelText('openai_base_url')).toHaveValue('https://api.deepseek.com/v1')
    expect(within(backup).getByLabelText('思考程度')).toHaveValue('high')
    // name 锁定：只读文本 + 提示，不出现输入框
    expect(within(backup).queryByLabelText('name')).not.toBeInTheDocument()
    expect(within(backup).getByText('名称不可修改，改名请删除后重建')).toBeInTheDocument()
    // key 未配置：占位符提示可在此设置
    expect(within(backup).getByLabelText('api_key')).toHaveAttribute('placeholder', '未配置，可在此设置')
  })

  it('api_key 留空时提交体不含 api_key；填了则包含', async () => {
    updateCredential.mockResolvedValue(OK)
    render(<SecretsForm status={STATUS} config={CONFIG} onSaved={() => {}} />)

    const backup = rowOf('deepseek-backup')
    fireEvent.click(within(backup).getByRole('button', { name: '编辑' }))
    fireEvent.change(within(backup).getByLabelText('model'), { target: { value: 'deepseek-v3' } })
    fireEvent.click(within(backup).getByRole('button', { name: '保存' }))

    await waitFor(() =>
      expect(updateCredential).toHaveBeenCalledWith('deepseek-backup', {
        provider: 'openai_compat',
        model: 'deepseek-v3',
        max_tokens: 8192,
        openai_base_url: 'https://api.deepseek.com/v1',
        thinking_effort: 'high',
      }),
    )
    // 保存成功后收起：卡片内不再有编辑表单
    await waitFor(() =>
      expect(within(backup).queryByLabelText('model')).not.toBeInTheDocument(),
    )

    // 再展开填 api_key：提交体携带该键（表单按 config 初值重挂载，model 回到初值）
    fireEvent.click(within(backup).getByRole('button', { name: '编辑' }))
    fireEvent.change(within(backup).getByLabelText('api_key'), { target: { value: 'sk-new' } })
    fireEvent.click(within(backup).getByRole('button', { name: '保存' }))

    await waitFor(() =>
      expect(updateCredential).toHaveBeenCalledWith('deepseek-backup', {
        provider: 'openai_compat',
        model: 'deepseek-chat',
        max_tokens: 8192,
        openai_base_url: 'https://api.deepseek.com/v1',
        thinking_effort: 'high',
        api_key: 'sk-new',
      }),
    )
  })

  it('再点「编辑」或表单「取消」可收起，不发起请求', () => {
    render(<SecretsForm status={STATUS} config={CONFIG} onSaved={() => {}} />)

    const backup = rowOf('deepseek-backup')
    fireEvent.click(within(backup).getByRole('button', { name: '编辑' }))
    expect(within(backup).getByLabelText('model')).toBeInTheDocument()
    fireEvent.click(within(backup).getByRole('button', { name: '编辑' }))
    expect(within(backup).queryByLabelText('model')).not.toBeInTheDocument()

    fireEvent.click(within(backup).getByRole('button', { name: '编辑' }))
    fireEvent.click(within(backup).getByRole('button', { name: '取消' }))
    expect(within(backup).queryByLabelText('model')).not.toBeInTheDocument()
    expect(updateCredential).not.toHaveBeenCalled()
  })

  it('default 合成凭证（config 无 credentials 段）：编辑初值回退 config.llm 平铺字段', () => {
    const legacyStatus: SecretsStatus = {
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
    const legacyConfig: AppConfig = {
      ...CONFIG,
      llm: {
        provider: 'anthropic',
        model: 'claude-sonnet-4-5',
        max_tokens: 2048,
        openai_base_url: '',
        thinking_effort: 'off',
        max_consecutive_failures: 3,
        // 无 credentials 段：旧版单凭证配置
      },
      agents: undefined,
    }
    render(<SecretsForm status={legacyStatus} config={legacyConfig} onSaved={() => {}} />)

    const card = rowOf('default')
    fireEvent.click(within(card).getByRole('button', { name: '编辑' }))

    expect(within(card).getByLabelText('model')).toHaveValue('claude-sonnet-4-5')
    expect(within(card).getByLabelText('max_tokens')).toHaveValue('2048')
    expect(within(card).getByLabelText('思考程度')).toHaveValue('off')
    // anthropic 不显示 base_url；key 已配置的占位符提示留空保持不变
    expect(within(card).queryByLabelText('openai_base_url')).not.toBeInTheDocument()
    expect(within(card).getByLabelText('api_key')).toHaveAttribute('placeholder', '已配置，留空保持不变')
  })
})

describe('SecretsForm(凭证管理) · 删除凭证', () => {
  it('未被引用的凭证可删除：confirm 后调 deleteCredential(name)，成功回调', async () => {
    vi.stubGlobal('confirm', vi.fn().mockReturnValue(true))
    deleteCredential.mockResolvedValue({ ...OK, key_saved: false })
    const onSaved = vi.fn()
    render(<SecretsForm status={STATUS} config={CONFIG} onSaved={onSaved} />)

    const backup = rowOf('deepseek-backup')
    fireEvent.click(within(backup).getByRole('button', { name: '删除' }))

    expect(window.confirm).toHaveBeenCalledWith('确认删除凭证「deepseek-backup」？')
    await waitFor(() => expect(deleteCredential).toHaveBeenCalledWith('deepseek-backup'))
    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1))
  })

  it('删除已生效但热重建失败：onSaved 真实刷新、被删卡片消失后警告条仍可见', async () => {
    vi.stubGlobal('confirm', vi.fn().mockReturnValue(true))
    deleteCredential.mockResolvedValue({ ...OK, key_saved: false, llm_error: 'provider 重建失败' })

    // onSaved 重新拉取状态，被删凭证从 props 消失；错误提示必须提升到列表级保留
    function Harness() {
      const [status, setStatus] = useState(STATUS)
      return (
        <SecretsForm
          status={status}
          config={CONFIG}
          onSaved={() =>
            setStatus((s) => ({
              ...s,
              credentials: (s.credentials ?? []).filter((c) => c.name !== 'deepseek-backup'),
            }))
          }
        />
      )
    }
    render(<Harness />)

    fireEvent.click(within(rowOf('deepseek-backup')).getByRole('button', { name: '删除' }))

    // 列表刷新后被删卡片消失，但 llm_error 琥珀警告条仍在列表级展示
    await waitFor(() => expect(screen.queryByText('deepseek-backup')).not.toBeInTheDocument())
    const alert = screen.getByRole('alert')
    expect(alert).toHaveTextContent('provider 重建失败')
    expect(alert).toHaveClass('text-amber-300')
  })

  it('仍被 agent 引用的凭证删除按钮禁用', () => {
    render(<SecretsForm status={STATUS} config={CONFIG} onSaved={() => {}} />)
    expect(within(rowOf('claude-main')).getByRole('button', { name: '删除' })).toBeDisabled()
  })

  it('confirm 取消时不发起请求', () => {
    vi.stubGlobal('confirm', vi.fn().mockReturnValue(false))
    render(<SecretsForm status={STATUS} config={CONFIG} onSaved={() => {}} />)
    fireEvent.click(within(rowOf('deepseek-backup')).getByRole('button', { name: '删除' }))
    expect(deleteCredential).not.toHaveBeenCalled()
  })
})

describe('SecretsForm(凭证管理) · 旧配置兼容', () => {
  it('credentials 为空数组：显示 default 引导提示并保留旧两输入框表单，不显示新增表单', () => {
    const legacy: SecretsStatus = { gate_key: true, llm_key: true, telegram: false, credentials: [] }
    render(<SecretsForm status={legacy} config={null} onSaved={() => {}} />)

    expect(screen.getByText(/旧版单凭证（default）配置/)).toBeInTheDocument()
    expect(screen.getByLabelText('ANTHROPIC_API_KEY')).toBeInTheDocument()
    expect(screen.getByLabelText('OPENAI_API_KEY')).toBeInTheDocument()
    expect(screen.queryByText('新增凭证')).not.toBeInTheDocument()
  })
})
