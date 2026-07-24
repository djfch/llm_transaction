/**
 * 工具调用垂直步骤链：seq 圆点+连线、工具名/入参/风控判定/结果/耗时逐步展示。
 * 复用型组件：实时决策轮（流式追加）与历史决策轮卡片共用；空数组显示等待提示。
 */
import { useState } from 'react'
import type { ToolCall } from '../../api/types'
import Badge from '../Badge'

/** 入参摘要截断阈值（字符数，超出显示「展开」按钮）；compact 模式更紧凑 */
const ARGS_CLIP = 120
const ARGS_CLIP_COMPACT = 72

/** 入参展示文本：对象→紧凑 JSON 单行，字符串原样 */
function argsText(args: ToolCall['args']): string {
  return typeof args === 'string' ? args : JSON.stringify(args)
}

/** 结果展示文本：对象→缩进 JSON，字符串原样 */
function resultText(result: ToolCall['result']): string {
  return typeof result === 'string' ? result : JSON.stringify(result, null, 2)
}

/** 风控判定徽标三态（与后端口径一致：空串=未入风控） */
function verdictBadge(verdict: string): { text: string; tone: 'ok' | 'danger' | 'neutral' } {
  if (verdict === 'deny') return { text: '风控拒绝', tone: 'danger' }
  if (verdict === 'allow') return { text: '风控放行', tone: 'ok' }
  return { text: '免判(未入风控)', tone: 'neutral' }
}

/** 入参行：等宽紧凑 JSON，超长截断，点击「展开/收起」切换全文 */
function ArgsLine({ text, clip }: { text: string; clip: number }) {
  const [open, setOpen] = useState(false)
  const long = text.length > clip
  const shown = open || !long ? text : `${text.slice(0, clip)}…`
  return (
    <div className="mt-1 break-all font-mono text-[11px] leading-5 text-zinc-500">
      {shown}
      {long && (
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          className="ml-1.5 text-violet-300/80 hover:text-violet-200"
        >
          {open ? '收起' : '展开'}
        </button>
      )}
    </div>
  )
}

/** 单步：圆点+连线+内容卡；deny 时整卡红色系并行内展示风控理由 */
function StepItem({ call, last, compact }: { call: ToolCall; last: boolean; compact: boolean }) {
  const denied = call.risk_verdict === 'deny'
  const badge = verdictBadge(call.risk_verdict)
  return (
    <li className="relative pb-3 pl-8 last:pb-0">
      {!last && (
        <span className="absolute bottom-0 left-[9.5px] top-6 w-px bg-gradient-to-b from-cyan-400/40 via-violet-400/20 to-transparent" />
      )}
      <span
        className={`absolute left-0 top-1 flex h-5 w-5 items-center justify-center rounded-full border text-[10px] ${
          denied
            ? 'border-rose-400/70 bg-rose-500/20 text-rose-300'
            : 'border-cyan-300/60 bg-cyan-400/15 text-cyan-300'
        }`}
      >
        {denied ? '✕' : '✓'}
      </span>
      <div
        className={`rounded-lg border ${
          denied ? 'border-rose-500/40 bg-rose-500/[.06]' : 'border-white/5 bg-white/[.02]'
        } ${compact ? 'px-2.5 py-1.5' : 'px-3 py-2'}`}
      >
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-mono text-[10px] text-zinc-600">seq {call.seq}</span>
          <span
            className={`font-mono text-[13px] font-semibold ${denied ? 'text-rose-200' : 'text-cyan-200'}`}
          >
            {call.tool}
          </span>
          <span className="ml-auto">
            <Badge text={badge.text} tone={badge.tone} />
          </span>
          <span className="font-mono tabular-nums text-[10px] text-zinc-500">
            {call.duration_ms}ms
          </span>
        </div>
        <ArgsLine text={argsText(call.args)} clip={compact ? ARGS_CLIP_COMPACT : ARGS_CLIP} />
        {denied && call.risk_reason !== '' && (
          <div className="mt-1 flex items-start gap-1.5 text-[11px] leading-5 text-rose-300">
            <span>⛔</span>
            <span>风控理由：{call.risk_reason}</span>
          </div>
        )}
        <details className="mt-1">
          <summary className="cursor-pointer list-none text-[11px] text-zinc-500 transition hover:text-violet-300">
            ▸ 执行结果
          </summary>
          <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap rounded bg-zinc-950/80 p-2 font-mono text-[11px] leading-5 text-zinc-400">
            {resultText(call.result)}
          </pre>
        </details>
      </div>
    </li>
  )
}

export default function ToolSteps({
  toolCalls,
  compact = false,
}: {
  toolCalls: ToolCall[]
  compact?: boolean
}) {
  if (toolCalls.length === 0) {
    return (
      <p className="rounded-lg border border-dashed border-violet-400/20 bg-violet-400/[.03] px-4 py-6 text-center text-xs text-zinc-500">
        等待 LLM 发起调用…
      </p>
    )
  }
  return (
    <ol className="relative">
      {toolCalls.map((c, i) => (
        <StepItem key={c.seq} call={c} last={i === toolCalls.length - 1} compact={compact} />
      ))}
    </ol>
  )
}
