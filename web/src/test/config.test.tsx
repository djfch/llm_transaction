/**
 * 配置中心测试：风控表单校验（纯函数 + 组件交互）。
 */
import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { RiskConfig } from '../api/types'
import RiskForm from '../pages/config/RiskForm'
import GeneralForm from '../pages/config/GeneralForm'
import { parseRisk, validateRisk } from '../pages/config/validate'

const baseRisk: RiskConfig = {
  max_position_pct: 0.3,
  max_total_position_pct: 0.8,
  max_leverage: 5,
  daily_loss_limit: 0.1,
  max_orders_per_day: 20,
  max_deviation: 0.02,
  kill_switch: false,
}

const validValues = {
  max_position_pct: '0.3',
  max_total_position_pct: '0.8',
  max_leverage: '5',
  daily_loss_limit: '0.1',
  max_orders_per_day: '20',
  max_deviation: '0.02',
}

describe('validateRisk(纯校验函数)', () => {
  it('合法输入通过校验并可解析为数字', () => {
    expect(validateRisk(validValues)).toEqual({})
    expect(parseRisk(validValues)).toEqual({
      max_position_pct: 0.3,
      max_total_position_pct: 0.8,
      max_leverage: 5,
      daily_loss_limit: 0.1,
      max_orders_per_day: 20,
      max_deviation: 0.02,
    })
  })

  it('拒绝越界值与"总仓小于单仓"', () => {
    const errors = validateRisk({ ...validValues, max_leverage: '0', max_total_position_pct: '0.2' })
    expect(errors.max_leverage).toBeTruthy()
    expect(errors.max_total_position_pct).toBe('总仓上限不能小于单仓上限')
    expect(parseRisk({ ...validValues, max_leverage: '0' })).toBeNull()
  })
})

describe('RiskForm(风控参数表单)', () => {
  it('输入非法值时展示错误并禁用保存，修正后可提交', async () => {
    const onSave = vi.fn().mockResolvedValue(undefined)
    render(<RiskForm initial={baseRisk} onSave={onSave} />)

    const leverageInput = screen.getByLabelText('最大杠杆倍数')
    const saveBtn = screen.getByRole('button', { name: /保存风控参数/ })

    // 非法值：0 → 报错 + 禁用保存
    fireEvent.change(leverageInput, { target: { value: '0' } })
    expect(await screen.findByText('杠杆上限需为 1–100 的整数')).toBeInTheDocument()
    expect(saveBtn).toBeDisabled()

    // 修正为合法值 → 错误消失，可保存
    fireEvent.change(leverageInput, { target: { value: '10' } })
    expect(screen.queryByText('杠杆上限需为 1–100 的整数')).not.toBeInTheDocument()
    expect(saveBtn).toBeEnabled()

    fireEvent.click(saveBtn)
    expect(onSave).toHaveBeenCalledTimes(1)
    // 提交内容：解析后的数字 + 保留未在表单内的 kill_switch 字段
    expect(onSave).toHaveBeenCalledWith({ ...baseRisk, max_leverage: 10 })
  })
})

describe('GeneralForm(常规设置表单)', () => {
  it('显示中文标签和模式选项，同时保留底层配置值', () => {
    const initial = {
      mode: 'paper',
      llm: {
        provider: 'anthropic',
        model: 'claude-test',
        max_tokens: 2048,
        openai_base_url: '',
        max_consecutive_failures: 3,
      },
      notify: { telegram_enabled: false },
      risk: baseRisk,
      scheduler: { default_wake_minutes: 60, min_wake_minutes: 5, max_wake_minutes: 720 },
    }
    render(<GeneralForm initial={initial} onSave={vi.fn()} />)

    expect(screen.getByText('运行模式')).toBeInTheDocument()
    expect(screen.getByRole('option', { name: '模拟盘' })).toHaveValue('paper')
    expect(screen.getByText('模型提供商')).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'Anthropic' })).toHaveValue('anthropic')
    expect(screen.getByText('最大输出长度（令牌）')).toBeInTheDocument()
    expect(screen.getByText('最大连续失败次数')).toBeInTheDocument()
    expect(screen.getByText('Telegram 通知')).toBeInTheDocument()
    expect(screen.queryByText(/llm\.|notify\.|openai_compat/)).not.toBeInTheDocument()
  })
})
