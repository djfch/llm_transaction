/**
 * 实时决策轮卡片头：脉冲呼吸点 + 轮次号 + agent 徽标 + 唤醒来源徽标（仅 trader）+ 走秒计时 + 起止时间行。
 * 从 LiveRoundHero 拆出（单文件 300 行硬上限）；agent/上轮文案映射与 wake_source 配色随之迁移。
 */
import type { AgentLiveRound, LiveAgentKind } from '../../api/types'
import { fmtTime } from '../../utils/format'

/** agent → 徽标文案与配色：交易 violet / 复盘 cyan / 研报 amber（用户可见仅中文） */
const AGENT_BADGE: Record<LiveAgentKind, { label: string; className: string }> = {
  trader: { label: '交易', className: 'border-violet-300/40 bg-violet-400/10 text-violet-300' },
  review: { label: '复盘', className: 'border-cyan-300/40 bg-cyan-400/10 text-cyan-300' },
  research: { label: '研报', className: 'border-amber-300/40 bg-amber-400/10 text-amber-300' },
}

/** agent → 非进行中时的「上轮」徽标文案 */
const AGENT_LAST_LABEL: Record<LiveAgentKind, string> = {
  trader: '上轮决策',
  review: '上轮复盘',
  research: '上轮研报',
}

/** wake_source(唤醒来源) → 徽标配色：价格触发 amber / 定时唤醒 cyan / 启动 violet */
function wakeClass(source: string): string {
  if (source.includes('价格')) return 'border-amber-300/40 bg-amber-400/10 text-amber-300'
  if (source.includes('定时')) return 'border-cyan-300/40 bg-cyan-400/10 text-cyan-300'
  if (source.includes('启动')) return 'border-violet-300/40 bg-violet-400/10 text-violet-300'
  return 'border-zinc-500/40 bg-zinc-500/10 text-zinc-400'
}

/** 秒数 → HH:MM:SS（计时展示，等宽数字） */
function fmtElapsed(sec: number): string {
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(Math.floor(sec / 3600))}:${pad(Math.floor((sec % 3600) / 60))}:${pad(Math.floor(sec % 60))}`
}

export default function HeroHeader({
  round,
  agent,
  inRound,
  elapsed,
}: {
  round: AgentLiveRound
  agent: LiveAgentKind
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
          className={`rounded border px-2 py-0.5 text-[11px] font-semibold ${AGENT_BADGE[agent].className}`}
        >
          {AGENT_BADGE[agent].label}
        </span>
        {agent === 'trader' && (
          <span
            className={`rounded border px-2 py-0.5 text-[11px] font-semibold ${wakeClass(round.wake_source)}`}
          >
            {round.wake_source}
          </span>
        )}
        {!inRound && (
          <span className="rounded border border-zinc-600/40 bg-zinc-700/30 px-2 py-0.5 text-[11px] text-zinc-400">
            {AGENT_LAST_LABEL[agent]}
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
        ) : inRound ? (
          <span className="text-violet-300">null（进行中）</span>
        ) : (
          'null' // 僵尸轮（超阈值未收尾）：原始值直出，不标进行中
        )}
      </div>
    </header>
  )
}
