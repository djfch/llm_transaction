/**
 * 完整对话消息流：llm_raw 经 buildConversation（Wave 1 契约）解析为 agent loop 消息。
 * assistant 紫色系卡片（思考文本 / 发起调用），user·工具返回青灰系卡片（deny 红色系+风控理由）；
 * 整体可折叠（标题「完整对话 · agent loop」+ 消息数徽标）；无消息时不渲染。
 */
import { useMemo } from 'react'
import type { ToolCall } from '../../api/types'
import type { ConversationMessage } from '../../utils/conversation'
import { buildConversation } from '../../utils/conversation'

/** assistant 消息卡：kind=text 显示思考/结论文本；kind=tool_call 显示「发起调用 工具名+参数摘要」 */
function AssistantBubble({ msg }: { msg: ConversationMessage }) {
  return (
    <div className="rounded-lg border border-violet-400/25 bg-violet-400/[.05] px-3 py-2">
      <div className="mb-1 text-[10px] font-bold tracking-widest text-violet-300/90">ASSISTANT</div>
      {msg.kind === 'text' ? (
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

export default function ConversationThread({
  llmRaw,
  toolCalls,
  defaultOpen = false,
}: {
  llmRaw: string
  toolCalls: ToolCall[]
  defaultOpen?: boolean
}) {
  // 消息流：llm_raw 逐回合解析 + 审计 tool_calls 按 seq 合并（Wave 1 冻结契约）
  const messages = useMemo(() => buildConversation(llmRaw, toolCalls), [llmRaw, toolCalls])
  if (messages.length === 0) return null
  return (
    <details open={defaultOpen} className="text-xs">
      <summary className="cursor-pointer list-none text-zinc-500 transition hover:text-violet-300">
        ▸ 完整对话 · agent loop
        <span className="ml-2 rounded border border-violet-400/30 bg-violet-400/10 px-1.5 py-px font-mono text-[10px] text-violet-300">
          {messages.length} 条消息
        </span>
      </summary>
      <div className="mt-2 space-y-2">
        {messages.map((m, i) =>
          m.role === 'assistant' ? (
            <AssistantBubble key={i} msg={m} />
          ) : (
            <ToolResultBubble key={i} msg={m} />
          ),
        )}
      </div>
    </details>
  )
}
