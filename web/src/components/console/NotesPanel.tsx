/**
 * Agent 笔记列表（方案 C 换皮）：紫左边条卡片流，time + content，最新在上，最多 10 条。
 * 数据由父级装配层下发（哑组件）；最新在前由 http 适配层保证（后端原样为正序，见 adaptNotes）。
 */
import type { Note } from '../../api/types'
import { fmtTime } from '../../utils/format'

/** 单条笔记卡片 */
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

export default function NotesPanel({ notes }: { notes: Note[] }) {
  const items = notes.slice(0, 10) // 最多 10 条，最新在上
  return (
    <section className="space-y-3">
      <div className="flex items-center gap-2">
        <h2 className="text-sm font-semibold text-zinc-300">Agent 笔记</h2>
        <span className="text-xs text-zinc-500">— 它写给自己的备忘录</span>
      </div>
      {items.length === 0 ? (
        <div className="rounded-xl border border-white/5 bg-zinc-900/60 p-8 text-center text-sm text-zinc-500 backdrop-blur">
          暂无笔记
        </div>
      ) : (
        items.map((n, i) => <NoteCard key={`${n.time}-${i}`} note={n} />)
      )}
    </section>
  )
}
