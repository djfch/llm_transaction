/**
 * LLM 密钥表单测试：两个 password 输入框渲染、空串字段剔除、error 展示、成功清空与警告。
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { SecretsStatus } from '../api/types'
import SecretsForm from '../pages/config/SecretsForm'

// 隔离 API 层：只桩掉 setSecrets（组件唯一依赖）
const { setSecrets } = vi.hoisted(() => ({ setSecrets: vi.fn() }))
vi.mock('../api', () => ({ api: { setSecrets } }))

const configured: SecretsStatus = { gate_key: true, llm_key: true, telegram: false }
const unconfigured: SecretsStatus = { gate_key: false, llm_key: false, telegram: false }

const anthropicLabel = 'ANTHROPIC_API_KEY'
const openaiLabel = 'OPENAI_API_KEY'
const saveBtnName = '保存 LLM 密钥'

beforeEach(() => {
  setSecrets.mockReset()
})

describe('SecretsForm(LLM 密钥表单)', () => {
  it('渲染两个 password 输入框与只读徽标，占位符随配置状态变化', () => {
    const { unmount } = render(<SecretsForm status={configured} onSaved={() => {}} />)
    const anthropic = screen.getByLabelText(anthropicLabel)
    const openai = screen.getByLabelText(openaiLabel)
    expect(anthropic).toHaveAttribute('type', 'password')
    expect(openai).toHaveAttribute('type', 'password')
    expect(anthropic).toHaveAttribute('autocomplete', 'new-password')
    expect(openai).toHaveAttribute('autocomplete', 'new-password')
    expect(anthropic).toHaveAttribute('placeholder', '已配置，输入以更换')
    // 交易所 / Telegram 只读徽标保留
    expect(screen.getByText('gate_key')).toBeInTheDocument()
    expect(screen.getByText('telegram')).toBeInTheDocument()

    unmount()
    render(<SecretsForm status={unconfigured} onSaved={() => {}} />)
    expect(screen.getByLabelText(anthropicLabel)).toHaveAttribute('placeholder', '未配置')
  })

  it('保存时剔除空串字段，仅提交已填写的 key', async () => {
    setSecrets.mockResolvedValue({ saved: true, llm_configured: true, error: '' })
    render(<SecretsForm status={configured} onSaved={() => {}} />)

    fireEvent.change(screen.getByLabelText(anthropicLabel), { target: { value: 'sk-ant-123' } })
    fireEvent.click(screen.getByRole('button', { name: saveBtnName }))

    await waitFor(() => expect(setSecrets).toHaveBeenCalledTimes(1))
    // openai 字段为空串，被剔除
    expect(setSecrets).toHaveBeenCalledWith({ anthropic_api_key: 'sk-ant-123' })
  })

  it('error 非空时展示玫瑰色错误条且不清空输入、不回调 onSaved', async () => {
    setSecrets.mockResolvedValue({
      saved: true,
      llm_configured: true,
      error: 'provider 重建失败：连接超时',
    })
    const onSaved = vi.fn()
    render(<SecretsForm status={configured} onSaved={onSaved} />)

    const input = screen.getByLabelText(anthropicLabel)
    fireEvent.change(input, { target: { value: 'sk-ant-123' } })
    fireEvent.click(screen.getByRole('button', { name: saveBtnName }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('provider 重建失败：连接超时')
    expect(alert).toHaveClass('text-rose-300')
    expect(input).toHaveValue('sk-ant-123')
    expect(onSaved).not.toHaveBeenCalled()
  })

  it('保存成功清空输入框并调用 onSaved 回调', async () => {
    setSecrets.mockResolvedValue({ saved: true, llm_configured: true, error: '' })
    const onSaved = vi.fn()
    render(<SecretsForm status={configured} onSaved={onSaved} />)

    const input = screen.getByLabelText(openaiLabel)
    fireEvent.change(input, { target: { value: 'sk-openai-1' } })
    fireEvent.click(screen.getByRole('button', { name: saveBtnName }))

    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1))
    expect(input).toHaveValue('')
    expect(setSecrets).toHaveBeenCalledWith({ openai_api_key: 'sk-openai-1' })
    expect(await screen.findByText('已保存到服务器 .env')).toBeInTheDocument()
  })

  it('llm_configured=false 时展示琥珀色警告', async () => {
    setSecrets.mockResolvedValue({ saved: true, llm_configured: false, error: '' })
    render(<SecretsForm status={unconfigured} onSaved={() => {}} />)

    fireEvent.change(screen.getByLabelText(anthropicLabel), { target: { value: 'sk-ant-123' } })
    fireEvent.click(screen.getByRole('button', { name: saveBtnName }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('LLM 仍未配置')
    expect(alert).toHaveClass('text-amber-300')
  })
})
