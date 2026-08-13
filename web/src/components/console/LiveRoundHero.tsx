/**
 * 实时决策轮主角卡：多 agent（交易/复盘/研报）实时轮流换展示——由 useLiveAgent 选定当前 agent
 * （后到优先、结束停留），经 getLiveFor 拉取归一快照；进行中每 3 秒轮询 + WS 六事件即时刷新，
 * 进行中直接渲染 live 快照里实时增长的 llm_raw；结束后 lazy 拉取审计详情校准终态，
 * 渲染本轮结论与可折叠的完整对话；紫色呼吸描边仅进行中启用（prefers-reduced-motion 兜底）。
 * 三 live 端点统一在轮结束后保留终态轮（结束事件后 reload 即得服务器终态）；
 * ended_at===null 但 started_at 超僵尸阈值的轮（进程崩溃残留）不视为进行中——不呼吸、不轮询。
 */
import { useEffect, useMemo, useState } from 'react'
import { api } from '../../api'
import type { RoundDetail } from '../../api/types'
import { useApiData } from '../../hooks/useApiData'
import { isLiveRoundEvent, useLiveAgent, ZOMBIE_MS } from '../../hooks/useLiveAgent'
import { useWs } from '../../hooks/useWs'
import { buildConversation } from '../../utils/conversation'
import StateHint from '../StateHint'
import ConversationThread from './ConversationThread'
import HeroHeader from './HeroHeader'
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

/** 本轮结束后 lazy 拉取审计详情校准终态；新一轮开始或进行中清空旧详情。 */
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

/** 结束区：审计详情加载状态与本轮结论；完整对话由主卡统一实时渲染。 */
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
    </div>
  )
}

export default function LiveRoundHero() {
  const { connected, lastMessage } = useWs()
  const currentAgent = useLiveAgent(lastMessage, connected)
  const { data, loading, error, reload } = useApiData(
    () => api.getLiveFor(currentAgent),
    [currentAgent],
  )
  const { data: status } = useApiData(() => api.getStatus(), [])
  // 三端点统一在轮结束后保留终态轮，live 快照即唯一展示来源（无本地兜底）
  const round = data?.round ?? null

  // 进行中口径三端点统一：round.ended_at === null 且未超僵尸阈值
  // （僵尸轮 = 进程崩溃残留的 ended_at=NULL 脏数据，不算进行中——不呼吸、不轮询）
  const inRound =
    round !== null && round.ended_at === null && Date.now() - round.started_at * 1000 <= ZOMBIE_MS
  const now = useNow(inRound)
  const ended = round !== null && round.ended_at !== null
  const { detail, detailError } = useRoundDetail(round?.round_id ?? '', ended)

  // 决策中每 3 秒轮询追加工具调用；空闲后停止，靠 WS 事件即时刷新
  useEffect(() => {
    if (!inRound) return
    const timer = setInterval(reload, 3000)
    return () => clearInterval(timer)
  }, [inRound, reload])

  // WS 六事件（三 agent 轮开始/结束）触发即时刷新：start 切换靠 currentAgent 变化自动重载，
  // end 停留时靠这里的 reload 拿服务器终态（三端点统一保留终态轮，结束后 round 不会变 null）
  useEffect(() => {
    if (lastMessage === null || !isLiveRoundEvent(lastMessage)) return
    reload()
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
  const shownRaw = detail?.llm_raw ?? round?.llm_raw ?? ''
  const shownPrompt = detail?.prompt_snapshot ?? round?.prompt_snapshot ?? ''
  const shownContext = detail?.context_snapshot ?? round?.context_snapshot ?? ''
  const allowCount = shownCalls.filter((c) => c.risk_verdict === 'allow').length
  const denyCount = shownCalls.filter((c) => c.risk_verdict === 'deny').length
  // llm_configured 是 trader 语义（自动决策暂停），仅展示 trader 轮时提示
  const llmOff = currentAgent === 'trader' && status !== null && !status.llm_configured
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
            <HeroHeader round={round} agent={currentAgent} inRound={inRound} elapsed={elapsed} />
            {llmOff && (
              <p className="mb-3 rounded-lg border border-amber-400/30 bg-amber-400/[.06] px-4 py-2.5 text-xs text-amber-300">
                LLM未配置：自动决策已暂停，请先在设置中配置 API Key
              </p>
            )}
            {round.error !== '' && (
              <p className="mb-3 rounded-lg border border-rose-500/30 bg-rose-500/[.06] px-4 py-2.5 text-xs text-rose-300">
                本轮错误：{round.error}
              </p>
            )}
            <div className="mb-2 flex items-center justify-between">
              <span className="text-[11px] tracking-widest text-cyan-300/80">
                工具调用链 · tool_calls
              </span>
              <span className="font-mono tabular-nums text-[10px] text-zinc-600">
                allow <span className="text-emerald-400">{allowCount}</span> / deny{' '}
                <span className={denyCount > 0 ? 'text-rose-400' : ''}>{denyCount}</span> / 共{' '}
                {shownCalls.length} 步
              </span>
            </div>
            <ToolSteps toolCalls={shownCalls} />
            <div className="mt-3 border-t border-white/5 pt-3">
              <ConversationThread
                llmRaw={shownRaw}
                toolCalls={shownCalls}
                promptSnapshot={shownPrompt}
                contextSnapshot={shownContext}
              />
            </div>
            {ended && (
              <EndedSection detail={detail} detailError={detailError} conclusion={conclusion} />
            )}
          </div>
        )}
      </StateHint>
    </section>
  )
}
