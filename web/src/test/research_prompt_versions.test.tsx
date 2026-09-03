/**
 * 研报提示词版本历史测试：列表（vN/来源徽标/状态徽标/当前/reason）、「当前」= 首个已生效版本
 * （草稿可能更新于已生效版本，首项是草稿时当前标在首个 applied）、回滚按钮只开在已生效且非当前行、
 * 点选两版本看 diff（+ 绿 / - 红）、回滚 confirm 流程（确认 → 成功提示 + 刷新 + onRolledBack；取消 → 不调接口）。
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../api/http'
import type { ResearchPromptVersion } from '../api/types'
import ResearchPromptVersions from '../pages/config/ResearchPromptVersions'

/** 版本夹具（最新在前）：v4 草稿最新但非当前；v3 为当前（首个已生效）；v2 已废弃；v1 已生效可回滚 */
const VERSIONS: ResearchPromptVersion[] = [
  { id: 4, md5: 'md5-d', createdBy: 'review_agent', reason: '复盘草稿：待生效', reviewReportId: 2, time: '2026-08-07T03:00:00.000Z', status: 'draft' },
  { id: 3, md5: 'md5-c', createdBy: 'review_agent', reason: '复盘：收紧高置信门槛', reviewReportId: 1, time: '2026-08-06T03:00:00.000Z', status: 'applied' },
  { id: 2, md5: 'md5-b', createdBy: 'review_agent', reason: '复盘草稿（已被 v3 取代）', reviewReportId: 1, time: '2026-08-05T03:00:00.000Z', status: 'discarded' },
  { id: 1, md5: 'md5-a', createdBy: 'human', reason: '初始版本', reviewReportId: null, time: '2026-08-04T03:00:00.000Z', status: 'applied' },
]

const holder = vi.hoisted(() => ({
  getResearchPromptVersions: vi.fn(),
  getResearchPromptDiff: vi.fn(),
  rollbackResearchPrompt: vi.fn(),
}))
vi.mock('../api', () => ({
  api: {
    getResearchPromptVersions: () => holder.getResearchPromptVersions(),
    getResearchPromptDiff: (from: number, to: number) => holder.getResearchPromptDiff(from, to),
    rollbackResearchPrompt: (id: number) => holder.rollbackResearchPrompt(id),
  },
}))

let versions: ResearchPromptVersion[]

beforeEach(() => {
  versions = [...VERSIONS]
  vi.clearAllMocks()
  holder.getResearchPromptVersions.mockImplementation(() => Promise.resolve([...versions]))
  holder.getResearchPromptDiff.mockImplementation((from: number, to: number) =>
    Promise.resolve(
      [`--- v${from}`, `+++ v${to}`, '-只输出白名单合约方向。', '+只输出白名单合约方向，宏观依据须注明兑现窗口。'].join('\n'),
    ),
  )
  holder.rollbackResearchPrompt.mockImplementation((id: number) => {
    const target = VERSIONS.find((v) => v.id === id)
    if (!target) return Promise.reject(new ApiError(404, `研报提示词版本不存在: ${id}`))
    versions = [
      { id: 5, md5: target.md5, createdBy: 'rollback', reason: `回滚到 v${id}`, reviewReportId: null, time: '2026-08-07T04:00:00.000Z', status: 'applied' },
      ...versions,
    ]
    return Promise.resolve({ rolledBackTo: id, version: 5 })
  })
})

afterEach(() => vi.unstubAllGlobals())

describe('ResearchPromptVersions(研报提示词版本历史)', () => {
  it('列表渲染：vN / 来源徽标 / 状态徽标 / reason；「当前」标在首个已生效版本（首项为草稿时亦然）', async () => {
    render(<ResearchPromptVersions />)

    expect(await screen.findByText('v4')).toBeInTheDocument()
    // 状态徽标：草稿 ×1 / 已废弃 ×1 / 已生效 ×2
    expect(screen.getByText('草稿')).toBeInTheDocument()
    expect(screen.getByText('已废弃')).toBeInTheDocument()
    expect(screen.getAllByText('已生效')).toHaveLength(2)
    // 来源徽标：复盘 ×3 + 人工 ×1
    expect(screen.getAllByText('复盘')).toHaveLength(3)
    expect(screen.getByText('人工')).toBeInTheDocument()
    // 当前标记仅一枚，且在 v3 行（v3 与 v4 的行文本唯一区分靠徽标，用行级断言）
    expect(screen.getAllByText('当前')).toHaveLength(1)
    const v3Row = screen.getByText('v3').closest('li')!
    expect(v3Row.textContent).toContain('当前')
    const v4Row = screen.getByText('v4').closest('li')!
    expect(v4Row.textContent).not.toContain('当前')
  })

  it('回滚按钮只开在已生效且非当前行（草稿/已废弃/当前行均无）', async () => {
    render(<ResearchPromptVersions />)
    await screen.findByText('v1')

    // 仅 v1（已生效、非当前）有回滚按钮
    expect(screen.getAllByRole('button', { name: '回滚到此版本' })).toHaveLength(1)
    expect(screen.getByText('v1').closest('li')!.textContent).toContain('回滚到此版本')
  })

  it('点选两版本：拉取 diff（旧 → 新），+ 绿 / - 红着色', async () => {
    render(<ResearchPromptVersions />)
    await screen.findByText('v4')

    fireEvent.click(screen.getByText('v2'))
    fireEvent.click(screen.getByText('v1'))

    await waitFor(() => expect(holder.getResearchPromptDiff).toHaveBeenCalledWith(1, 2))
    expect(await screen.findByText('diff v1 → v2')).toBeInTheDocument()
    expect(screen.getByText('+只输出白名单合约方向，宏观依据须注明兑现窗口。')).toHaveClass('text-emerald-300')
    expect(screen.getByText('-只输出白名单合约方向。')).toHaveClass('text-rose-300')
    expect(screen.getByText('--- v1')).toHaveClass('text-zinc-500')
    expect(screen.getByText('+++ v2')).toHaveClass('text-zinc-500')
  })

  it('两版本内容一致时 diff 区给出明确提示而非空白', async () => {
    holder.getResearchPromptDiff.mockImplementationOnce(() => Promise.resolve(''))
    render(<ResearchPromptVersions />)
    await screen.findByText('v4')

    fireEvent.click(screen.getByText('v2'))
    fireEvent.click(screen.getByText('v1'))

    await waitFor(() => expect(holder.getResearchPromptDiff).toHaveBeenCalledWith(1, 2))
    expect(await screen.findByText('两版本内容一致')).toBeInTheDocument()
  })

  it('回滚确认：提示成功、刷新版本列表（新 applied 版本标当前）、通知宿主刷新提示词内容', async () => {
    const confirmMock = vi.fn().mockReturnValue(true)
    vi.stubGlobal('confirm', confirmMock)
    const onRolledBack = vi.fn()
    render(<ResearchPromptVersions onRolledBack={onRolledBack} />)
    await screen.findByText('v1')
    const callsBefore = holder.getResearchPromptVersions.mock.calls.length

    fireEvent.click(screen.getByRole('button', { name: '回滚到此版本' }))

    expect(confirmMock).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(holder.rollbackResearchPrompt).toHaveBeenCalledWith(1))
    expect(await screen.findByText('已回滚到 v1（生成新版本 v5）')).toBeInTheDocument()
    await waitFor(() => expect(holder.getResearchPromptVersions).toHaveBeenCalledTimes(callsBefore + 1))
    await waitFor(() => expect(onRolledBack).toHaveBeenCalledTimes(1))

    // 刷新后 v5（回滚来源、applied）置顶并标当前
    expect(await screen.findByText('v5')).toBeInTheDocument()
    expect(screen.getByText('回滚')).toBeInTheDocument()
    const v5Row = screen.getByText('v5').closest('li')!
    expect(v5Row.textContent).toContain('当前')
    expect(screen.getAllByText('当前')).toHaveLength(1)
  })

  it('回滚取消：confirm 返回 false 时不调接口', async () => {
    const confirmMock = vi.fn().mockReturnValue(false)
    vi.stubGlobal('confirm', confirmMock)
    render(<ResearchPromptVersions />)
    await screen.findByText('v1')

    fireEvent.click(screen.getByRole('button', { name: '回滚到此版本' }))

    expect(confirmMock).toHaveBeenCalledTimes(1)
    expect(holder.rollbackResearchPrompt).not.toHaveBeenCalled()
  })

  it('回滚失败：展示 ApiError.detail', async () => {
    const confirmMock = vi.fn().mockReturnValue(true)
    vi.stubGlobal('confirm', confirmMock)
    holder.rollbackResearchPrompt.mockRejectedValueOnce(new ApiError(404, '研报提示词版本不存在: 1'))
    render(<ResearchPromptVersions />)
    await screen.findByText('v1')

    fireEvent.click(screen.getByRole('button', { name: '回滚到此版本' }))

    expect(await screen.findByText('回滚失败：研报提示词版本不存在: 1')).toBeInTheDocument()
  })
})
