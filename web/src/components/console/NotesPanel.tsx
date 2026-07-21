/**
 * Agent 笔记面板：服务端分页展示最新笔记，并在决策轮结束时刷新当前页。
 * 面板独立管理请求状态，避免 ConsolePage 为不同页面重复加载整份笔记列表。
 */
import { useEffect, useState } from 'react'
import { api } from '../../api'
import type { Note } from '../../api/types'
import { useApiData } from '../../hooks/useApiData'
import { usePageState } from '../../hooks/usePageState'
import { useWs } from '../../hooks/useWs'
import { fmtTime } from '../../utils/format'
import StateHint from '../StateHint'
import PaginationControls from './PaginationControls'

/** Agent 笔记面板每页条数固定为 4，使右侧面板与时间线的视觉高度更协调。 */
const PAGE_SIZE = 4

/** 单条笔记卡片：显示记录时间和 Agent 原始备忘内容。 */
function NoteCard({ note }: { note: Note }) {
  return (
    <article className="rounded-xl border border-white/5 border-l-2 border-l-violet-400/60 bg-zinc-900/60 p-3.5 backdrop-blur">
      <div className="mb-1.5 font-mono text-[10px] tabular-nums text-zinc-500">
        {fmtTime(note.time)}
      </div>
      <p className="text-[13px] leading-6 text-zinc-300">{note.content}</p>
    </article>
  )
}

/** 分页展示 Agent 笔记，最新笔记固定从第一页开始读取。 */
export default function NotesPanel() {
  const [total, setTotal] = useState(0)
  const pagination = usePageState(total, PAGE_SIZE)
  const query = useApiData(
    () => api.getNotes(pagination.page * PAGE_SIZE, PAGE_SIZE),
    [pagination.page],
  )
  const { lastMessage } = useWs()
  const { reload } = query

  useEffect(() => {
    if (query.data) setTotal(query.data.total)
  }, [query.data])

  useEffect(() => {
    if (lastMessage?.type === 'round') reload()
  }, [lastMessage, reload])

  const items = query.data?.items ?? []

  return (
    <section className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="text-sm font-semibold text-zinc-300">Agent 笔记</h2>
        <span className="text-xs text-zinc-500">— 它写给自己的备忘录</span>
        <span className="ml-auto text-xs tabular-nums text-zinc-500">共 {total} 条笔记</span>
      </div>
      <StateHint loading={query.loading} error={query.error} empty={total === 0}>
        <div className="space-y-3">
          {items.map((note, index) => (
            <NoteCard key={`${note.time}-${index}`} note={note} />
          ))}
        </div>
        <PaginationControls
          page={pagination.page}
          total={total}
          pageSize={PAGE_SIZE}
          itemLabel="笔记"
          loading={query.loading}
          onPageChange={pagination.goToPage}
        />
      </StateHint>
    </section>
  )
}
