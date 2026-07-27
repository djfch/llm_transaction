/**
 * 策略版本历史测试：列表（vN/来源徽标/当前/reason）、点选两版本看 diff（+ 绿 / - 红）、
 * 回滚 confirm 流程（确认 → 成功提示 + 刷新 + onRolledBack；取消 → 不调接口）。
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../api/http'
import type { StrategyVersion } from '../api/types'
import StrategyVersions from '../pages/config/StrategyVersions'

/** 版本夹具（最新在前）：v3 为当前（md5 唯一） */
const VERSIONS: StrategyVersion[] = [
  { id: 3, md5: 'md5-c', createdBy: 'review_agent', reason: '复盘：收紧止损', reportId: 2, time: '2026-07-27T03:00:00.000Z' },
  { id: 2, md5: 'md5-b', createdBy: 'review_agent', reason: '复盘：延长持仓', reportId: 1, time: '2026-07-26T03:00:00.000Z' },
  { id: 1, md5: 'md5-a', createdBy: 'human', reason: '初始版本', reportId: null, time: '2026-07-25T03:00:00.000Z' },
]

const holder = vi.hoisted(() => ({
  getStrategyVersions: vi.fn(),
  getStrategyDiff: vi.fn(),
  rollbackStrategy: vi.fn(),
}))
vi.mock('../api', () => ({
  api: {
    getStrategyVersions: () => holder.getStrategyVersions(),
    getStrategyDiff: (from: number, to: number) => holder.getStrategyDiff(from, to),
    rollbackStrategy: (id: number) => holder.rollbackStrategy(id),
  },
}))

let versions: StrategyVersion[]

beforeEach(() => {
  versions = [...VERSIONS]
  vi.clearAllMocks()
  holder.getStrategyVersions.mockImplementation(() => Promise.resolve([...versions]))
  holder.getStrategyDiff.mockImplementation((from: number, to: number) =>
    Promise.resolve(
      [`--- v${from}`, `+++ v${to}`, '@@ -1 +1 @@', '-保本优先。', '+保本优先，严格止损。'].join('\n'),
    ),
  )
  holder.rollbackStrategy.mockImplementation((id: number) => {
    const target = VERSIONS.find((v) => v.id === id)
    if (!target) return Promise.reject(new ApiError(404, `策略版本不存在: ${id}`))
    versions = [
      { id: 4, md5: target.md5, createdBy: 'rollback', reason: `回滚到 v${id}`, reportId: null, time: '2026-07-27T04:00:00.000Z' },
      ...versions,
    ]
    return Promise.resolve({ rolledBackTo: id, version: 4 })
  })
})

afterEach(() => vi.unstubAllGlobals())

describe('StrategyVersions(策略版本历史)', () => {
  it('列表渲染：vN / 来源徽标 / reason / 当前标记；当前行无回滚按钮', async () => {
    render(<StrategyVersions />)

    expect(await screen.findByText('v3')).toBeInTheDocument()
    expect(screen.getByText('v2')).toBeInTheDocument()
    expect(screen.getByText('v1')).toBeInTheDocument()
    // 来源徽标：复盘 ×2 + 人工 ×1
    expect(screen.getAllByText('复盘').length).toBe(2)
    expect(screen.getByText('人工')).toBeInTheDocument()
    // 当前标记仅 v3 一枚
    expect(screen.getAllByText('当前')).toHaveLength(1)
    expect(screen.getByText('初始版本')).toBeInTheDocument()
    // 非当前两行才有回滚按钮
    expect(screen.getAllByRole('button', { name: '回滚到此版本' })).toHaveLength(2)
  })

  it('点选两版本：拉取 diff（旧 → 新），+ 绿 / - 红着色', async () => {
    render(<StrategyVersions />)
    await screen.findByText('v3')

    fireEvent.click(screen.getByText('v2'))
    fireEvent.click(screen.getByText('v1'))

    await waitFor(() => expect(holder.getStrategyDiff).toHaveBeenCalledWith(1, 2))
    expect(await screen.findByText('diff v1 → v2')).toBeInTheDocument()
    expect(screen.getByText('+保本优先，严格止损。')).toHaveClass('text-emerald-300')
    expect(screen.getByText('-保本优先。')).toHaveClass('text-rose-300')
    // 文件头行灰显（不着红/绿）
    expect(screen.getByText('--- v1')).toHaveClass('text-zinc-500')
    expect(screen.getByText('+++ v2')).toHaveClass('text-zinc-500')
  })

  it('两版本内容一致时 diff 区给出明确提示而非空白', async () => {
    holder.getStrategyDiff.mockImplementationOnce(() => Promise.resolve(''))
    render(<StrategyVersions />)
    await screen.findByText('v3')

    fireEvent.click(screen.getByText('v2'))
    fireEvent.click(screen.getByText('v1'))

    await waitFor(() => expect(holder.getStrategyDiff).toHaveBeenCalledWith(1, 2))
    expect(await screen.findByText('两版本内容一致')).toBeInTheDocument()
  })

  it('切换版本对拉取新 diff 期间不残留旧 diff 行', async () => {
    let resolveDiff: ((value: string) => void) | null = null
    render(<StrategyVersions />)
    await screen.findByText('v3')

    // 第一对：v1 ↔ v2，旧 diff 渲染完毕
    fireEvent.click(screen.getByText('v2'))
    fireEvent.click(screen.getByText('v1'))
    expect(await screen.findByText('-保本优先。')).toBeInTheDocument()

    // 点 v3 挤掉最早选择 → 新对 v1 ↔ v3，第二次拉取挂起
    holder.getStrategyDiff.mockImplementationOnce(
      () =>
        new Promise<string>((resolve) => {
          resolveDiff = resolve
        }),
    )
    fireEvent.click(screen.getByText('v3'))
    await waitFor(() => expect(holder.getStrategyDiff).toHaveBeenCalledWith(1, 3))

    // 新 diff 未返回前不得残留旧 diff 行（避免误读为当前版本对的差异）
    expect(screen.queryByText('-保本优先。')).not.toBeInTheDocument()
    expect(screen.getByText('diff 加载中…')).toBeInTheDocument()

    resolveDiff!('+ 新内容\n')
    expect(await screen.findByText('+ 新内容')).toBeInTheDocument()
  })

  it('回滚确认：提示成功、刷新版本列表（新版本标当前）、通知宿主刷新策略内容', async () => {
    const confirmMock = vi.fn().mockReturnValue(true)
    vi.stubGlobal('confirm', confirmMock)
    const onRolledBack = vi.fn()
    render(<StrategyVersions onRolledBack={onRolledBack} />)
    await screen.findByText('v1')
    const callsBefore = holder.getStrategyVersions.mock.calls.length

    // 回滚按钮顺序与行序一致：[v2, v1]，点 v1 行
    fireEvent.click(screen.getAllByRole('button', { name: '回滚到此版本' })[1])

    expect(confirmMock).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(holder.rollbackStrategy).toHaveBeenCalledWith(1))
    expect(await screen.findByText('已回滚到 v1（生成新版本 v4）')).toBeInTheDocument()
    await waitFor(() => expect(holder.getStrategyVersions).toHaveBeenCalledTimes(callsBefore + 1))
    await waitFor(() => expect(onRolledBack).toHaveBeenCalledTimes(1))

    // 刷新后 v4（回滚来源）置顶并标当前；与 v1 同 md5 也只有一枚当前标记
    expect(await screen.findByText('v4')).toBeInTheDocument()
    expect(screen.getByText('回滚')).toBeInTheDocument()
    expect(screen.getAllByText('当前')).toHaveLength(1)
  })

  it('回滚取消：confirm 返回 false 时不调接口', async () => {
    const confirmMock = vi.fn().mockReturnValue(false)
    vi.stubGlobal('confirm', confirmMock)
    render(<StrategyVersions />)
    await screen.findByText('v2')

    fireEvent.click(screen.getAllByRole('button', { name: '回滚到此版本' })[0])

    expect(confirmMock).toHaveBeenCalledTimes(1)
    expect(holder.rollbackStrategy).not.toHaveBeenCalled()
  })

  it('回滚失败：展示 ApiError.detail', async () => {
    const confirmMock = vi.fn().mockReturnValue(true)
    vi.stubGlobal('confirm', confirmMock)
    holder.rollbackStrategy.mockRejectedValueOnce(new ApiError(404, '策略版本不存在: 2'))
    render(<StrategyVersions />)
    await screen.findByText('v2')

    fireEvent.click(screen.getAllByRole('button', { name: '回滚到此版本' })[0])

    expect(await screen.findByText('回滚失败：策略版本不存在: 2')).toBeInTheDocument()
  })
})
