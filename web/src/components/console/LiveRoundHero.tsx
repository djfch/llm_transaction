/**
 * 实时决策轮主角卡：agent 决策中流式展示工具调用链（进行中每 3 秒轮询 + WS 事件即时刷新），
 * ended_at 从 null 变为非 null 后 lazy 拉取审计详情（llm_raw 仅详情接口有完整版），
 * 渲染本轮结论与可折叠的完整对话；紫色呼吸描边仅进行中启用（prefers-reduced-motion 兜底）。
 */
import { useEffect, useMemo, useState } from 'react'
import { api } from '../../api'
import type { AgentLiveRound, RoundDetail } from '../../api/types'
import { useApiData } from '../../hooks/useApiData'
import { useWs } from '../../hooks/useWs'
import { buildConversation } from '../../utils/conversation'
import { fmtTime, wakeSourceLabel } from '../../utils/format'
import StateHint from '../StateHint'
import ConversationThread from './ConversationThread'
import ToolSteps from './ToolSteps'

/** 呼吸描边动画（进行中）：命名加 lrh- 前缀避免与全局样式冲突；减少动效偏好时关闭 */
const HERO_STYLE = `
@keyframes lrh-breathe {
  0%,100% { box-shadow: 0 0 0 1px rgba(167,139,250,.35), 0 0 24px rgba(167,139,250,.10); }
  50%     { box-shadow: 0 0 0 1px rgba(167,139,250,.70), 0 0 42px rgba(167,139,250,.22); }
}
.lrh-breathe { animation: lrh-breathe 2.6s ease-in-out infinite; }
@media (prefers-reduced-motion: reduce) { .lrh-breathe { animation: none !important; } }
`

/** 走秒计时：进行中每秒刷新当前时间（用于「已进行 HH:MM:SS」） */
function useNow(active: boolean): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!active) return
    const timer = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [active])
  return now
}

/** 本轮结束后 lazy 拉取审计详情（llm_raw + 完整 tool_calls）；新一轮开始/进行中清空 */
function useRoundDetail(roundId: string, ended: boolean) {
  const [detail, setDetail] = useState<RoundDetail | null>(null)
  const [detailError, setDetailError] = useState<string | null>(null)
  useEffect(() => {
    if (roundId === '' || !ended) {
      setDetail(null)
      setDetailError(null)
      return
    }
    let alive = true
    api
      .getRound(roundId)
      .then((d) => {
        if (alive) setDetail(d)
      })
      .catch((e: unknown) => {
        if (alive) setDetailError(e instanceof Error ? e.message : String(e))
      })
    return () => {
      alive = false
    }
  }, [roundId, ended])
  return { detail, detailError }
}

/** 秒数 → HH:MM:SS（计时展示，等宽数字） */
function fmtElapsed(sec: number): string {
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(Math.floor(sec / 3600))}:${pad(Math.floor((sec % 3600) / 60))}:${pad(Math.floor(sec % 60))}`
}

/** wake_source(唤醒来源) → 徽标配色：价格触发 amber / 定时唤醒 cyan / 启动 violet */
function wakeClass(source: string): string {
  const lower = source.toLowerCase()
  if (source.includes('价格') || lower.includes('price')) return 'border-amber-300/40 bg-amber-400/10 text-amber-300'
  if (source.includes('定时') || lower.includes('timer')) return 'border-cyan-300/40 bg-cyan-400/10 text-cyan-300'
  if (source.includes('启动') || lower.includes('start')) return 'border-violet-300/40 bg-violet-400/10 text-violet-300'
  return 'border-zinc-500/40 bg-zinc-500/10 text-zinc-400'
}

/** 卡片头：脉冲呼吸点 + 轮次号 + 唤醒来源徽标 + 走秒计时 + 起止时间行 */
function HeroHeader({
  round,
  inRound,
  elapsed,
}: {
  round: AgentLiveRound
  inRound: boolean
  elapsed: number
}) {
  return (
    <header className="mb-4 border-b border-white/5 pb-3">
      <div className="flex flex-wrap items-center gap-3">
        <span className="relative flex h-3 w-3">
          {inRound && (
            <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-violet-400 opacity-70 motion-reduce:animate-none" />
          )}
          <span
            className={`relative inline-flex h-3 w-3 rounded-full ${inRound ? 'bg-violet-400' : 'bg-zinc-600'}`}
          />
        </span>
        <h2 className="text-lg font-bold text-zinc-50">
          实时决策轮 <span className="font-mono text-violet-300">#{round.round_id}</span>
        </h2>
        <span
          className={`rounded border px-2 py-0.5 text-[11px] font-semibold ${wakeClass(round.wake_source)}`}
        >
          {wakeSourceLabel(round.wake_source)}
        </span>
        {!inRound && (
          <span className="rounded border border-zinc-600/40 bg-zinc-700/30 px-2 py-0.5 text-[11px] text-zinc-400">
            上轮决策
          </span>
        )}
        <span className="ml-auto font-mono tabular-nums text-xs text-zinc-500">
          {inRound ? '已进行' : '耗时'}{' '}
          <span className={inRound ? 'text-violet-300' : 'text-zinc-300'}>
            {fmtElapsed(elapsed)}
          </span>
        </span>
      </div>
      <div className="mt-2 font-mono tabular-nums text-[11px] text-zinc-600">
        开始 {fmtTime(new Date(round.started_at * 1000).toISOString())} · 结束{' '}
        {round.ended_at !== null ? (
          fmtTime(new Date(round.ended_at * 1000).toISOString())
        ) : (
          <span className="text-violet-300">进行中</span>
        )}
      </div>
    </header>
  )
}

/** 结束区：审计详情加载中/失败提示、本轮结论（末条 assistant 文本）、完整对话（默认收起） */
function EndedSection({
  detail,
  detailError,
  conclusion,
}: {
  detail: RoundDetail | null
  detailError: string | null
  conclusion: string
}) {
  if (detailError !== null) {
    return (
      <p className="mt-3 rounded-lg border border-rose-500/30 bg-rose-500/[.06] px-4 py-2.5 text-xs text-rose-300">
        审计详情加载失败：{detailError}
      </p>
    )
  }
  if (detail === null) {
    return <p className="mt-3 text-center text-xs text-zinc-500">本轮已结束，正在加载完整对话…</p>
  }
  return (
    <div className="mt-3 space-y-3 border-t border-white/5 pt-3">
      {conclusion !== '' && (
        <div className="rounded-lg border border-violet-400/30 bg-violet-400/[.06] px-4 py-3">
          <div className="mb-1 text-[10px] font-bold tracking-widest text-violet-300/90">
            本轮结论
          </div>
          <p className="whitespace-pre-wrap text-[13px] leading-6 text-zinc-200">{conclusion}</p>
        </div>
      )}
      <ConversationThread llmRaw={detail.llm_raw} toolCalls={detail.tool_calls} />
    </div>
  )
}

export default function LiveRoundHero() {
  const { data, loading, error, reload } = useApiData(() => api.getAgentLive(), [])
  const { data: status } = useApiData(() => api.getStatus(), [])
  const { lastMessage } = useWs()
  const inRound = data?.in_round ?? false
  const round = data?.round ?? null
  const now = useNow(inRound)
  const ended = round !== null && round.ended_at !== null
  const { detail, detailError } = useRoundDetail(round?.round_id ?? '', ended)

  // 决策中每 3 秒轮询追加工具调用；空闲后停止，靠 WS 事件即时刷新
  useEffect(() => {
    if (!inRound) return
    const timer = setInterval(reload, 3000)
    return () => clearInterval(timer)
  }, [inRound, reload])

  // WS round_start(轮开始)/round(轮结束) 消息触发即时刷新
  useEffect(() => {
    if (lastMessage?.type === 'round_start' || lastMessage?.type === 'round') reload()
    // eslint-disable-next-line react-hooks/exhaustive-deps -- 只跟随消息变化
  }, [lastMessage])

  // 本轮结论：详情对话消息流中最后一条 assistant 文本
  const conclusion = useMemo(() => {
    if (detail === null) return ''
    const msgs = buildConversation(detail.llm_raw, detail.tool_calls)
    return (
      [...msgs].reverse().find((m) => m.role === 'assistant' && m.kind === 'text')?.text ?? ''
    )
  }, [detail])

  // 工具链：结束后用审计详情的完整链，否则用实时快照（进行中流式追加）
  const shownCalls = detail?.tool_calls ?? data?.tool_calls ?? []
  const allowCount = shownCalls.filter((c) => c.risk_verdict === 'allow').length
  const denyCount = shownCalls.filter((c) => c.risk_verdict === 'deny').length
  const llmOff = status !== null && !status.llm_configured
  const endMs = ended && round.ended_at !== null ? round.ended_at * 1000 : now
  const elapsed = round ? Math.max(0, Math.floor(endMs / 1000 - round.started_at)) : 0

  return (
    <section
      className={`relative overflow-hidden rounded-2xl border bg-zinc-950/70 p-5 shadow-xl shadow-black/30 ${
        inRound ? 'lrh-breathe border-violet-400/30' : 'border-white/10'
      }`}
    >
      <style>{HERO_STYLE}</style>
      {/* 仅首次加载显示加载态，轮询刷新不清空已有内容 */}
      <StateHint loading={loading && data === null} error={error}>
        {round === null ? (
          data !== null && (
            <p className="py-10 text-center text-sm text-zinc-500">
              暂无决策记录：agent 尚未执行过决策轮
            </p>
          )
        ) : (
          <div>
            <HeroHeader round={round} inRound={inRound} elapsed={elapsed} />
            {llmOff && (
              <p className="mb-3 rounded-lg border border-amber-400/30 bg-amber-400/[.06] px-4 py-2.5 text-xs text-amber-300">
                LLM 未配置：自动决策已暂停，请先在设置中配置 API 密钥
              </p>
            )}
            {round.error !== '' && (
              <p className="mb-3 rounded-lg border border-rose-500/30 bg-rose-500/[.06] px-4 py-2.5 text-xs text-rose-300">
                本轮错误：{round.error}
              </p>
            )}
            <div className="mb-2 flex items-center justify-between">
              <span className="text-[11px] tracking-widest text-cyan-300/80">
                工具调用链
              </span>
              <span className="font-mono tabular-nums text-[10px] text-zinc-600">
                放行 <span className="text-emerald-400">{allowCount}</span> / 拒绝{' '}
                <span className={denyCount > 0 ? 'text-rose-400' : ''}>{denyCount}</span> / 共{' '}
                {shownCalls.length} 步
              </span>
            </div>
            <ToolSteps toolCalls={shownCalls} />
            {ended && (
              <EndedSection detail={detail} detailError={detailError} conclusion={conclusion} />
            )}
          </div>
        )}
      </StateHint>
    </section>
  )
}
