/**
 * 决策时间线卡片：摘要行（短号/唤醒来源徽标/策略版本徽标/已完成徽标/时间/summary），点击展开审计详情。
 * 归属笔记的轮在 summary 下嵌紫色左边条 + 斜体引文块。
 * 策略版本徽标由父级注入的 resolveStrategyVersion 按 strategyMd5 join（vN · 来源；空串/无匹配显示「—」）；
 * 展开后 lazy 拉取 getRound（卡片生命周期内缓存），渲染两个折叠区：
 * 「工具调用详情」→ ToolSteps(compact)；「完整对话」→ ConversationThread（自带折叠）。
 */
import { useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { api } from '../../api'
import type { RoundDetail, RoundSummary, StrategyVersion } from '../../api/types'
import { fmtClock, fmtTime, shortRoundId, strategyCreatorText } from '../../utils/format'
import ClampText from './ClampText'
import ConversationThread from './ConversationThread'
import ModelBadge from './ModelBadge'
import ToolSteps from './ToolSteps'

/** 归属本轮的 Agent 笔记（引文块数据；随 /rounds 当前页下发的 round.note 直传） */
export interface RoundNote {
  content: string
  time: string
}

interface TimelineCardProps {
  round: RoundSummary
  note?: RoundNote // 归属笔记（无归属不渲染引文块）
  expanded: boolean // 受控展开（父级手风琴 + focus 定位共用）
  highlight: boolean // focus 定位描边高亮（约 2s，附 jump-hl 类供测试/样式锚定）
  resolveStrategyVersion: (md5: string) => StrategyVersion | null // 策略版本 join（父级统一拉取版本表）
  onToggle: () => void
  cardRef: (el: HTMLElement | null) => void // 锚定（scrollIntoView 用）
}

/** 版本标签统一文案：vN · 来源（人工/复盘/回滚）；无关联显示「—」 */
function strategyVersionText(version: StrategyVersion | null): string {
  return version ? `v${version.id} · ${strategyCreatorText(version.createdBy)}` : '—'
}

/** 策略版本徽标：中性灰（元信息，点击不跳转） */
function StrategyVersionBadge({ version }: { version: StrategyVersion | null }) {
  return (
    <span className="rounded border border-zinc-600/50 bg-zinc-700/30 px-2 py-0.5 font-mono text-[10px] text-zinc-400">
      {strategyVersionText(version)}
    </span>
  )
}

/** wake_source → 徽标样式：定时灰 / 价格青 / 手动·启动·其他紫（文案保留原始来源） */
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

/** 折叠区：标题行（旋转箭头 + 文案）+ 内容（导出供 ReviewToolChain 复用同一折叠形态） */
export function FoldSection({
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
  resolveStrategyVersion,
  onToggle,
  cardRef,
}: TimelineCardProps) {
  const [detail, setDetail] = useState<RoundDetail | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [toolsOpen, setToolsOpen] = useState(true) // 展开默认打开工具区，便于定位工具调用

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
          <span className={wakeBadgeClass(round.wake_source)}>{round.wake_source}</span>
          <StrategyVersionBadge version={resolveStrategyVersion(round.strategyMd5)} />
          {/* 模型徽标：模型名为空（历史轮无记录）时 ModelBadge 自行不渲染 */}
          <ModelBadge
            model={round.llmModel}
            thinkingEffort={round.llmThinkingEffort}
            credentialName={round.llmCredentialName}
            provider={round.llmProvider}
          />
          {/* 历史轮都是已完成，使用灰字小徽标 */}
          <span className="text-[11px] text-zinc-600">已完成</span>
          <span className="ml-auto font-mono text-[11px] tabular-nums text-zinc-500">
            {fmtTime(round.started_at)}
          </span>
          <span className={`text-[10px] text-zinc-600 transition-transform ${expanded ? 'rotate-90' : ''}`}>
            ▸
          </span>
        </div>
      </button>

      {/* summary 在按钮外渲染：ClampText 自带展开按钮，不能嵌套进手风琴 button */}
      <div className="mt-2">
        <ClampText
          text={round.summary}
          clampClass="line-clamp-5"
          className="text-[13px] leading-6 text-zinc-300"
        />
      </div>

      {/* 归属笔记引文：紫色左边条 + 斜体；引文只读，超长按 5 行折叠 */}
      {note && (
        <blockquote className="mt-3 border-l-2 border-violet-400/50 pl-3 text-[13px] italic leading-6 text-violet-200/80">
          <ClampText text={`“${note.content}”`} clampClass="line-clamp-5" />
          <span className="text-[10px] not-italic text-zinc-600">
            —— Agent 笔记 · {fmtClock(note.time)}
          </span>
        </blockquote>
      )}

      {expanded && (
        <div className="mt-3 border-t border-zinc-800/80 pt-3">
          {loading && <p className="py-3 text-xs text-zinc-500">审计详情加载中…</p>}
          {error && <p className="py-3 text-xs text-rose-400">加载失败：{error}</p>}
          {detail && (
            <>
              <p className="mb-2 font-mono text-[11px] text-zinc-500">
                策略版本：{strategyVersionText(resolveStrategyVersion(detail.strategyMd5))}
                {detail.llmModel !== '' && (
                  <>
                    {'　'}模型：{detail.llmModel}
                    {detail.llmThinkingEffort !== '' ? ` · ${detail.llmThinkingEffort}` : ''}
                    {detail.llmCredentialName !== '' ? `（凭证 ${detail.llmCredentialName}）` : ''}
                  </>
                )}
              </p>
              <FoldSection
                title={`工具调用详情 · tool_calls（${detail.tool_calls.length} 步）`}
                open={toolsOpen}
                onToggle={() => setToolsOpen((o) => !o)}
              >
                <ToolSteps toolCalls={detail.tool_calls} compact />
              </FoldSection>
              {/* 完整对话由 ConversationThread 自身折叠，默认收起 */}
              <div className="mt-2">
                <ConversationThread
                  llmRaw={detail.llm_raw}
                  toolCalls={detail.tool_calls}
                  promptSnapshot={detail.prompt_snapshot}
                  contextSnapshot={detail.context_snapshot}
                />
              </div>
            </>
          )}
        </div>
      )}
    </article>
  )
}
