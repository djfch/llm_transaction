/** 配置中心研报调度表单：总开关、预设、自定义时间与保存行为。 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { ResearchScheduleConfig, ResearchScheduleStatus } from '../api/types'
import ResearchScheduleForm from '../pages/config/ResearchScheduleForm'

const initial: ResearchScheduleConfig = {
  enabled: false,
  schedules: [
    { id: 'asia_open', kind: 'market_open', market: 'XTKS', enabled: true, lead_minutes: 30 },
    { id: 'europe_open', kind: 'market_open', market: 'XLON', enabled: true, lead_minutes: 30 },
    { id: 'us_open', kind: 'market_open', market: 'XNYS', enabled: true, lead_minutes: 30 },
  ],
}

const status: ResearchScheduleStatus = {
  enabled: false,
  items: [
    { id: 'asia_open', kind: 'market_open', enabled: true, next_run_at: 1_788_201_000 },
    { id: 'europe_open', kind: 'market_open', enabled: true, next_run_at: 1_788_226_200 },
    { id: 'us_open', kind: 'market_open', enabled: true, next_run_at: 1_788_249_600 },
  ],
  calendar: {
    state: 'fallback',
    last_refreshed_at: 1_788_000_000,
    errors: { XLON: '页面结构变化' },
    warning: '官方日历不可确认的工作日按交易日执行',
  },
}

describe('ResearchScheduleForm(研报自动执行配置)', () => {
  it('展示三个市场预设、下一次执行和日历降级警告', () => {
    render(<ResearchScheduleForm initial={initial} status={status} onSave={vi.fn()} />)
    expect(screen.getByText('亚盘 · 东京')).toBeInTheDocument()
    expect(screen.getByText('欧盘 · 伦敦')).toBeInTheDocument()
    expect(screen.getByText('美盘 · 纽约')).toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent('官方日历不可确认')
    expect(screen.getAllByText(/下次/)).toHaveLength(3)
  })

  it('可开启总开关、暂停预设、添加自定义时间并仅保存 research 段', async () => {
    const onSave = vi.fn<(research: ResearchScheduleConfig) => Promise<void>>(async () => undefined)
    render(<ResearchScheduleForm initial={initial} status={null} onSave={onSave} />)

    fireEvent.click(screen.getByRole('checkbox', { name: '启用自动研报' }))
    fireEvent.click(screen.getByRole('checkbox', { name: '启用亚盘调度' }))
    fireEvent.click(screen.getByRole('button', { name: '添加时间点' }))
    fireEvent.change(screen.getByLabelText('自定义执行时间 1'), { target: { value: '12:30' } })
    fireEvent.change(screen.getByLabelText('自定义日期规则 1'), { target: { value: 'XNYS' } })
    fireEvent.click(screen.getByRole('button', { name: '保存研报设置' }))

    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1))
    const saved = onSave.mock.calls[0][0] as ResearchScheduleConfig
    expect(saved.enabled).toBe(true)
    expect(saved.schedules[0].enabled).toBe(false)
    expect(saved.schedules.at(-1)).toMatchObject({
      kind: 'fixed_time',
      enabled: true,
      time: '12:30',
      calendar: 'XNYS',
    })
    expect(screen.getByText(/已生效/)).toBeInTheDocument()
  })

  it('自定义时间可删除', () => {
    const withCustom: ResearchScheduleConfig = {
      ...initial,
      schedules: [
        ...initial.schedules,
        {
          id: '00000000-0000-4000-8000-000000000001',
          kind: 'fixed_time',
          enabled: true,
          time: '12:30',
          calendar: 'daily',
        },
      ],
    }
    render(<ResearchScheduleForm initial={withCustom} status={status} onSave={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: '删除自定义时间 1' }))
    expect(screen.queryByLabelText('自定义执行时间 1')).not.toBeInTheDocument()
  })

  it('预设停用后允许自定义项复用该预设时间', async () => {
    const onSave = vi.fn<(research: ResearchScheduleConfig) => Promise<void>>(async () => undefined)
    render(<ResearchScheduleForm initial={initial} status={status} onSave={onSave} />)

    fireEvent.click(screen.getByRole('checkbox', { name: '启用亚盘调度' }))
    fireEvent.click(screen.getByRole('button', { name: '添加时间点' }))
    fireEvent.change(screen.getByLabelText('自定义执行时间 1'), { target: { value: '07:30' } })
    fireEvent.click(screen.getByRole('button', { name: '保存研报设置' }))

    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1))
    expect(onSave.mock.calls[0][0].schedules.at(-1)).toMatchObject({ time: '07:30' })
  })

  it('启用预设时拒绝冲突时间，保存失败时保留未保存内容和后端原因', async () => {
    const onSave = vi.fn<(research: ResearchScheduleConfig) => Promise<void>>()
    render(<ResearchScheduleForm initial={initial} status={null} onSave={onSave} />)
    fireEvent.click(screen.getByRole('button', { name: '添加时间点' }))
    fireEvent.change(screen.getByLabelText('自定义执行时间 1'), { target: { value: '07:30' } })
    fireEvent.click(screen.getByRole('button', { name: '保存研报设置' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('可能与市场预设冲突')
    expect(onSave).not.toHaveBeenCalled()
    expect(screen.getByLabelText('自定义执行时间 1')).toHaveValue('07:30')

    fireEvent.change(screen.getByLabelText('自定义执行时间 1'), { target: { value: '12:31' } })
    onSave.mockRejectedValueOnce(new Error('服务端拒绝测试'))
    fireEvent.click(screen.getByRole('button', { name: '保存研报设置' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('服务端拒绝测试')
    expect(screen.getByLabelText('自定义执行时间 1')).toHaveValue('12:31')
  })
})
