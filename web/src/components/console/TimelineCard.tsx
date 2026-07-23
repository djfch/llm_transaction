/**
 * 决策时间线卡片：摘要行（短号/唤醒来源徽标/已完成徽标/时间/summary），点击展开审计详情。
 * 归属笔记的轮在 summary 下嵌引文块（紫色左边条 + 斜体，方案 C 同款）。
 * 展开后 lazy 拉取 getRound（卡片生命周期内缓存），渲染两个折叠区：
 * 「工具调用详情」→ ToolSteps(compact)；「完整对话」→ ConversationThread（自带折叠）。
 */
import { useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { api } from '../../api'
import type { RoundDetail, RoundSummary } from '../../api/types'
import { fmtClock, fmtTime, shortRoundId, wakeSourceLabel } from '../../utils/format'
import ConversationThread from './ConversationThread'
import ToolSteps from './ToolSteps'

/** 归属本轮的 Agent 笔记（引文块数据；由父级 round_id 映射注入） */
export interface RoundNote {
  content: string
  time: string
}

interface TimelineCardProps {
  round: RoundSummary
  note?: RoundNote // 归属笔记（无归属不渲染引文块）
  expanded: boolean // 受控展开（父级手风琴 + focus 定位共用）
  highlight: boolean // focus 定位描边高亮（约 2s，附 jump-hl 类供测试/样式锚定）
  onToggle: () => void
  cardRef: (el: HTMLElement | null) => void // 锚定（scrollIntoView 用）
}

/** wake_source → 徽标样式：定时灰 / 价格青 / 手动·启动·其他紫。 */
function wakeBadgeClass(source: string): string {
  const base = 'rounded border px-2 py-0.5 text-[10px] font-medium'
  const lower = source.toLowerCase()
  if (source.includes('定时') || lower.includes('schedul')) {
    return `${base} border-zinc-600/50 bg-zinc-700/30 text-zinc-400`
  }
  if (source.includes('价格') || lower.includes('price')) {
    return `${base} border-cyan-400/40 bg-cyan-400/10 text-cyan-300`
  }
  return `${base} border-violet-400/40 bg-violet-400/10 text-violet-300`
}

/** 折叠区：标题行（旋转箭头 + 文案）+ 内容 */
function FoldSection({
  title,
  open,
  onToggle,
  children,
}: {
  title: string
  open: boolean
  onToggle: () => void
  children: ReactNode
}) {
  return (
    <div className="mt-2">
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={open}
        className="flex items-center gap-1 text-xs text-zinc-500 transition hover:text-violet-300"
      >
        <span className={`inline-block transition-transform ${open ? 'rotate-90' : ''}`}>▸</span>
        {title}
      </button>
      {open && <div className="mt-2">{children}</div>}
    </div>
  )
}

export default function TimelineCard({
  round,
  note,
  expanded,
  highlight,
  onToggle,
  cardRef,
}: TimelineCardProps) {
  const [detail, setDetail] = useState<RoundDetail | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [toolsOpen, setToolsOpen] = useState(true) // 展开默认打开工具区（对齐设计稿跳转行为）

  // 展开时 lazy 拉取审计详情（detail 即缓存，卡片不卸载则不重拉）。
  // 注意：loading 不能进 deps —— setLoading 触发的重渲染会执行上一次 effect 的清理（alive=false），
  // 把进行中的 fetch 自我取消；用 fetchedRef 防重入（StrictMode 双调用也只拉一次）。
  const fetchedRef = useRef(false)
  useEffect(() => {
    if (!expanded || fetchedRef.current) return
    fetchedRef.current = true
    let alive = true
    setLoading(true)
    setError(null)
    api
      .getRound(round.round_id)
      .then((d) => {
        if (alive) setDetail(d)
      })
      .catch((e: unknown) => {
        if (alive) {
          setError(e instanceof Error ? e.message : String(e))
          fetchedRef.current = false // 失败允许收起后再展开重试
        }
      })
      .finally(() => {
        if (alive) setLoading(false)
      })
    return () => {
      alive = false
    }
  }, [expanded, round.round_id])

  const cardCls = [
    'round-card rounded-xl border bg-zinc-900/60 p-4 transition-shadow',
    highlight
      ? 'jump-hl border-violet-400/70 shadow-[0_0_36px_rgba(167,139,250,.35)]'
      : 'border-zinc-800',
  ].join(' ')

  return (
    <article ref={cardRef} data-round-id={round.round_id} className={cardCls}>
      <button type="button" onClick={onToggle} aria-expanded={expanded} className="block w-full text-left">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-mono text-sm font-bold text-zinc-200">#{shortRoundId(round.round_id)}</span>
          <span className={wakeBadgeClass(round.wake_source)}>{wakeSourceLabel(round.wake_source)}</span>
          {/* 历史轮都是已完成（静态文案，方案 C 同款灰字小徽标） */}
          <span className="text-[11px] text-zinc-600">已完成</span>
          <span className="ml-auto font-mono text-[11px] tabular-nums text-zinc-500">
            {fmtTime(round.started_at)}
          </span>
          <span className={`text-[10px] text-zinc-600 transition-transform ${expanded ? 'rotate-90' : ''}`}>
            ▸
          </span>
        </div>
        <p className="mt-2 text-[13px] leading-6 text-zinc-300">{round.summary}</p>
      </button>

      {/* 归属笔记引文：紫色左边条 + 斜体（方案 C 同款）；引文只读，不参与折叠交互 */}
      {note && (
        <blockquote className="mt-3 border-l-2 border-violet-400/50 pl-3 text-[13px] italic leading-6 text-violet-200/80">
          “{note.content}”
          <span className="ml-2 text-[10px] not-italic text-zinc-600">
            —— 代理笔记 · {fmtClock(note.time)}
          </span>
        </blockquote>
      )}

      {expanded && (
        <div className="mt-3 border-t border-zinc-800/80 pt-3">
          {loading && <p className="py-3 text-xs text-zinc-500">审计详情加载中…</p>}
          {error && <p className="py-3 text-xs text-rose-400">加载失败：{error}</p>}
          {detail && (
            <>
              <FoldSection
                title={`工具调用详情（${detail.tool_calls.length} 步）`}
                open={toolsOpen}
                onToggle={() => setToolsOpen((o) => !o)}
              >
                <ToolSteps toolCalls={detail.tool_calls} compact />
              </FoldSection>
              {/* 完整对话：ConversationThread 自带折叠（默认收起），不再外包 FoldSection */}
              <div className="mt-2">
                <ConversationThread llmRaw={detail.llm_raw} toolCalls={detail.tool_calls} />
              </div>
            </>
          )}
        </div>
      )}
    </article>
  )
}
