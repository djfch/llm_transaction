/**
 * 完整对话消息流：llm_raw 经 buildConversation（Wave 1 契约）解析为 agent loop 消息。
 * assistant 紫色系卡片（思考文本 / 发起调用），user·工具返回青灰系卡片（deny 红色系+风控理由）；
 * 整体可折叠（标题「完整对话 · agent loop」+ 消息数徽标）；无消息时不渲染。
 */
import { useMemo } from 'react'
import type { ToolCall } from '../../api/types'
import type { ConversationMessage, ConversationTurn } from '../../utils/conversation'
import { buildConversationTurns } from '../../utils/conversation'

/** assistant 消息卡：区分明文思考、回复文本与工具调用。 */
function AssistantBubble({ msg }: { msg: ConversationMessage }) {
  const isText = msg.kind === 'text' || msg.kind === 'reasoning'
  return (
    <div className="rounded-lg border border-violet-400/25 bg-violet-400/[.05] px-3 py-2">
      <div className="mb-1 text-[10px] font-bold tracking-widest text-violet-300/90">
        {msg.kind === 'reasoning' ? 'ASSISTANT · 思考过程' : 'ASSISTANT'}
      </div>
      {isText ? (
        <p className="whitespace-pre-wrap text-[12px] leading-5 text-zinc-300">{msg.text}</p>
      ) : (
        <p className="break-all font-mono text-[11px] leading-5 text-cyan-200/80">
          → 发起调用 {msg.text}
        </p>
      )}
    </div>
  )
}

/** user·工具返回消息卡：显示返回内容；riskVerdict='deny' 时红色系并附风控理由 */
function ToolResultBubble({ msg }: { msg: ConversationMessage }) {
  const denied = msg.riskVerdict === 'deny'
  return (
    <div
      className={`ml-5 rounded-lg border px-3 py-2 ${
        denied ? 'border-rose-500/30 bg-rose-500/[.05]' : 'border-cyan-400/15 bg-cyan-400/[.03]'
      }`}
    >
      <div
        className={`mb-1 text-[10px] font-bold tracking-widest ${
          denied ? 'text-rose-300/80' : 'text-cyan-300/70'
        }`}
      >
        USER · 工具返回 {msg.toolName}
        {denied ? '（风控拒绝）' : ''}
      </div>
      <p
        className={`whitespace-pre-wrap break-all font-mono text-[11px] leading-5 ${
          denied ? 'text-rose-200/90' : 'text-zinc-400'
        }`}
      >
        {msg.text}
      </p>
      {denied && msg.riskReason && (
        <p className="mt-1 text-[11px] leading-5 text-rose-300">
          风控理由：{msg.riskReason}
        </p>
      )}
    </div>
  )
}

/** 首次请求折叠区：展示真正发送给模型的 SYSTEM 提示词与 USER 上下文。 */
function InitialRequest({ system, user }: { system: string; user: string }) {
  if (system === '' && user === '') return null
  return (
    <details className="rounded-lg border border-white/10 bg-white/[.02] px-3 py-2">
      <summary className="cursor-pointer list-none text-[11px] text-zinc-400 hover:text-violet-300">
        ▸ 首次发送给 LLM · SYSTEM + USER
      </summary>
      <div className="mt-2 space-y-2">
        {system !== '' && <MessageText label="SYSTEM" text={system} />}
        {user !== '' && <MessageText label="USER · 初始上下文" text={user} />}
      </div>
    </details>
  )
}

/** 带角色标签的完整文本块；不截断，由外层 details 控制展开。 */
function MessageText({ label, text }: { label: string; text: string }) {
  return (
    <div className="rounded border border-white/5 bg-zinc-950/60 p-2">
      <div className="mb-1 text-[10px] font-bold tracking-widest text-zinc-500">{label}</div>
      <pre className="whitespace-pre-wrap break-words font-sans text-[12px] leading-5 text-zinc-300">
        {text}
      </pre>
    </div>
  )
}

/** 单个 LLM 回合折叠区：思考、回复、调用与返回按原顺序完整展示。 */
function TurnSection({ turn, index }: { turn: ConversationTurn; index: number }) {
  const rejected = turn.status === 'rejected'
  return (
    <details
      className={`rounded-lg border px-2.5 py-2 ${
        rejected ? 'border-amber-400/25 bg-amber-400/[.03]' : 'border-violet-400/15 bg-violet-400/[.02]'
      }`}
    >
      <summary
        className={`cursor-pointer list-none text-[11px] ${rejected ? 'text-amber-300/90' : 'text-violet-300/80'}`}
      >
        ▸ LLM 第 {index + 1} 次响应 · {turn.messages.length} 条内容
        {rejected ? ' · 已拒绝（工具未执行）' : ''}
      </summary>
      <div className="mt-2 space-y-2">
        {rejected && turn.error && (
          <p className="rounded border border-amber-400/15 bg-amber-950/20 px-2 py-1 text-[11px] text-amber-200/80">
            拒绝原因：{turn.error}
          </p>
        )}
        {turn.messages.map((message, messageIndex) =>
          message.role === 'assistant' ? (
            <AssistantBubble key={messageIndex} msg={message} />
          ) : (
            <ToolResultBubble key={messageIndex} msg={message} />
          ),
        )}
      </div>
    </details>
  )
}

export default function ConversationThread({
  llmRaw,
  toolCalls,
  promptSnapshot = '',
  contextSnapshot = '',
  defaultOpen = false,
}: {
  llmRaw: string
  toolCalls: ToolCall[]
  promptSnapshot?: string
  contextSnapshot?: string
  defaultOpen?: boolean
}) {
  const turns = useMemo(() => buildConversationTurns(llmRaw, toolCalls), [llmRaw, toolCalls])
  const messageCount = turns.reduce((count, turn) => count + turn.messages.length, 0)
  if (turns.length === 0 && promptSnapshot === '' && contextSnapshot === '') return null
  return (
    <details open={defaultOpen} className="text-xs">
      <summary className="cursor-pointer list-none text-zinc-500 transition hover:text-violet-300">
        ▸ 完整对话 · agent loop
        <span className="ml-2 rounded border border-violet-400/30 bg-violet-400/10 px-1.5 py-px font-mono text-[10px] text-violet-300">
          {messageCount} 条对话
        </span>
      </summary>
      <div className="mt-2 space-y-2">
        <InitialRequest system={promptSnapshot} user={contextSnapshot} />
        {turns.map((turn, index) => (
          <TurnSection key={index} turn={turn} index={index} />
        ))}
      </div>
    </details>
  )
}
