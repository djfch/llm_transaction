/**
 * 实时决策卡：agent 决策中实时展示当前轮的 prompt / 上下文 / 工具调用（每 3 秒轮询追加）；
 * 空闲时展示上一轮同样内容（徽标区分"上轮决策"）；WS round 消息触发即时刷新。
 */
import { useEffect } from 'react'
import { api } from '../api'
import type { AgentLiveRound } from '../api/types'
import { useApiData } from '../hooks/useApiData'
import { useWs } from '../hooks/useWs'
import { fmtTime } from '../utils/format'
import Badge from './Badge'
import Card from './Card'
import StateHint from './StateHint'
import ToolCallItem from './ToolCallItem'

/** 决策中徽标：info 色 + 脉冲圆点（与 Badge 的 info 色调一致） */
function LiveBadge() {
  return (
    <span className="inline-flex items-center gap-1.5 rounded border border-sky-500/30 bg-sky-500/15 px-2 py-0.5 text-xs font-medium text-sky-400">
      <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-sky-400" />
      决策中…
    </span>
  )
}

/** 头部行：round_id 短码（前 8 位）+ 唤醒来源 + 开始时间 */
function RoundHeader({ round }: { round: AgentLiveRound }) {
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm">
      <span className="font-mono text-xs text-slate-400">#{round.round_id.slice(0, 8)}</span>
      <span className="text-slate-400">wake_source(唤醒来源)={round.wake_source}</span>
      <span className="ml-auto text-xs tabular-nums text-slate-500">
        {fmtTime(new Date(round.started_at * 1000).toISOString())}
      </span>
    </div>
  )
}

/** 文本快照折叠区：摘要行显示字符数，默认收起 */
function SnapshotDetails({ label, text }: { label: string; text: string }) {
  return (
    <details className="rounded-lg border border-slate-800 bg-slate-900/60">
      <summary className="cursor-pointer list-none px-4 py-3 text-sm text-slate-400">
        {label}（{text.length} 字符）
      </summary>
      <pre className="max-h-72 overflow-auto whitespace-pre-wrap border-t border-slate-800 bg-slate-950 px-4 py-3 text-xs leading-relaxed text-slate-300">
        {text}
      </pre>
    </details>
  )
}

/** LLM 原始输出：进行中（空串）显示等待提示，完成后可展开 */
function LlmRawDetails({ llmRaw, inRound }: { llmRaw: string; inRound: boolean }) {
  if (inRound && llmRaw === '') {
    return (
      <div className="rounded-lg border border-slate-800 bg-slate-900/60 px-4 py-3 text-sm text-slate-500">
        llm_raw(LLM原始输出)：等待 LLM 输出…
      </div>
    )
  }
  return <SnapshotDetails label="llm_raw(LLM原始输出)" text={llmRaw} />
}

export default function LiveRoundCard() {
  const { data, loading, error, reload } = useApiData(() => api.getAgentLive(), [])
  const { lastMessage } = useWs()
  const inRound = data?.in_round ?? false

  // 决策中每 3 秒轮询追加；转为空闲（in_round=false）后清除定时器
  useEffect(() => {
    if (!inRound) return
    const timer = setInterval(reload, 3000)
    return () => clearInterval(timer)
  }, [inRound, reload])

  // WS round_start(轮开始)/round(轮结束) 消息都触发即时刷新
  useEffect(() => {
    if (lastMessage?.type === 'round_start' || lastMessage?.type === 'round') reload()
    // eslint-disable-next-line react-hooks/exhaustive-deps -- 只跟随消息变化
  }, [lastMessage])

  const round = data?.round ?? null
  const toolCalls = data?.tool_calls ?? []

  return (
    <Card
      title="实时决策 live"
      extra={data ? inRound ? <LiveBadge /> : <Badge text="上轮决策" /> : null}
    >
      {/* 仅首次加载显示加载态，轮询刷新不清空已有内容 */}
      <StateHint loading={loading && data === null} error={error}>
        {round === null ? (
          data !== null && <p className="py-8 text-center text-sm text-slate-500">暂无决策记录</p>
        ) : (
          <div className="space-y-3">
            <RoundHeader round={round} />
            {round.error !== '' && (
              <p className="rounded-lg border border-rose-500/30 bg-rose-500/10 px-4 py-3 text-xs text-rose-300">
                error(本轮错误)：{round.error}
              </p>
            )}
            <SnapshotDetails label="prompt_snapshot(完整Prompt)" text={round.prompt_snapshot} />
            <SnapshotDetails label="context_snapshot(上下文)" text={round.context_snapshot} />
            <LlmRawDetails llmRaw={round.llm_raw} inRound={inRound} />
            <div>
              <div className="mb-2 text-xs text-slate-500">
                tool_calls(工具调用) · {toolCalls.length} 次
              </div>
              <div className="space-y-2">
                {toolCalls.map((c) => (
                  <ToolCallItem key={c.seq} call={c} />
                ))}
              </div>
            </div>
          </div>
        )}
      </StateHint>
    </Card>
  )
}
