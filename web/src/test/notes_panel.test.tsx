/** Agent 笔记面板测试：服务端分页、总数、跳页校验与空态。 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { Note, NotesPageResult, WsMessage } from '../api/types'
import NotesPanel from '../components/console/NotesPanel'

/** 12 条笔记覆盖三页内容。 */
const NOTES: Note[] = Array.from({ length: 12 }, (_, index) => ({
  time: new Date(1_700_000_000_000 - index * 60_000).toISOString(),
  content: `第 ${12 - index} 条笔记`,
  round_id: '',
}))

/** 将笔记数组切片为 API 分页结果。 */
function pageOf(items: Note[], offset: number, limit: number): NotesPageResult {
  return { items: items.slice(offset, offset + limit), total: items.length, offset, limit }
}

const holder = vi.hoisted(() => ({ getNotes: vi.fn() }))
vi.mock('../api', () => ({
  api: { getNotes: (offset?: number, limit?: number) => holder.getNotes(offset, limit) },
}))

/** WebSocket 桩允许验证新决策后的当前页刷新。 */
const wsHolder = vi.hoisted(() => ({ lastMessage: null as WsMessage | null }))
vi.mock('../hooks/useWs', () => ({
  useWs: () => ({ connected: true, lastMessage: wsHolder.lastMessage }),
}))

let currentNotes: Note[]

beforeEach(() => {
  currentNotes = [...NOTES]
  wsHolder.lastMessage = null
  vi.clearAllMocks()
  holder.getNotes.mockImplementation((offset = 0, limit = 20): Promise<NotesPageResult> =>
    Promise.resolve(pageOf(currentNotes, offset, limit)),
  )
})

describe('NotesPanel(Agent 笔记)', () => {
  it('每页显示 4 条、显示总数，并支持下一页与指定页跳转', async () => {
    render(<NotesPanel />)

    expect(await screen.findByText('第 12 条笔记')).toBeInTheDocument()
    expect(screen.queryByText('第 8 条笔记')).not.toBeInTheDocument()
    expect(screen.getByText('共 12 条笔记')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '下一页' }))
    expect(await screen.findByText('第 8 条笔记')).toBeInTheDocument()
    expect(holder.getNotes).toHaveBeenLastCalledWith(4, 4)

    fireEvent.change(screen.getByLabelText('跳转到第几页笔记'), { target: { value: '3' } })
    fireEvent.click(screen.getByRole('button', { name: '跳转' }))
    expect(await screen.findByText('第 2 条笔记')).toBeInTheDocument()
    expect(screen.getByText('第 3/3 页 · 共 12 条笔记')).toBeInTheDocument()
  })

  it('非法跳转保留当前页且不发起额外请求', async () => {
    render(<NotesPanel />)
    await screen.findByText('第 12 条笔记')
    const callsBefore = holder.getNotes.mock.calls.length

    fireEvent.change(screen.getByLabelText('跳转到第几页笔记'), { target: { value: '4' } })
    fireEvent.click(screen.getByRole('button', { name: '跳转' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('请输入 1 至 3 的整数页码')
    expect(holder.getNotes).toHaveBeenCalledTimes(callsBefore)
    expect(screen.getByText('第 12 条笔记')).toBeInTheDocument()
  })

  it('空笔记仅显示总数与空态，不渲染分页控件', async () => {
    currentNotes = []
    render(<NotesPanel />)

    expect(await screen.findByText('暂无数据')).toBeInTheDocument()
    expect(screen.getByText('共 0 条笔记')).toBeInTheDocument()
    expect(screen.queryByRole('navigation', { name: '笔记分页' })).not.toBeInTheDocument()
  })

  it('round 事件刷新当前页与总数', async () => {
    const { rerender } = render(<NotesPanel />)
    await screen.findByText('第 12 条笔记')
    currentNotes = [{ time: new Date().toISOString(), content: '新笔记', round_id: '' }, ...currentNotes]
    wsHolder.lastMessage = { type: 'round', data: { round_id: 'r-new', ok: true, wake_source: '手动唤醒' } }
    rerender(<NotesPanel />)

    await waitFor(() => expect(holder.getNotes).toHaveBeenCalledTimes(2))
    expect(await screen.findByText('共 13 条笔记')).toBeInTheDocument()
    expect(screen.getByText('新笔记')).toBeInTheDocument()
  })
})
