/**
 * 决策轮「完整对话」构建：把 llm_raw 解析为消息流，供对话视图渲染。
 *
 * 后端事实：llm_raw = 每个 assistant 回合的原生 API 响应 JSON（单行紧凑）按 \n 连接；
 * 工具执行结果不在 llm_raw 里，而在审计 tool_calls（按 seq 顺序与回合内 tool_use 依次对应）。
 * 支持 Anthropic 原生格式与 OpenAI 兼容格式；解析失败时降级为「原文 + 工具调用链」。
 */
import type { ToolCall } from '../api/types'

/** 对话视图消息：assistant 的文本/工具调用 + user 角色的工具结果 */
export interface ConversationMessage {
  role: 'assistant' | 'user'
  kind: 'text' | 'tool_call' | 'tool_result'
  text: string // text: 思考/结论文本；tool_call: 工具名+参数摘要；tool_result: 返回内容
  toolName?: string
  riskVerdict?: string // tool_result 上携带，'deny' 时前端渲染红色
  riskReason?: string
}

/** 单回合解析产物：若干文本块 + 若干工具调用（参数已序列化为摘要文本） */
interface AssistantTurn {
  texts: string[]
  calls: Array<{ name: string; argsText: string }>
}

/** 摘要截断长度：参数/结果文本超过即截断，避免超长内容撑爆渲染 */
const MAX_TEXT = 500

/** 超长截断（末尾加省略号） */
function clip(s: string): string {
  return s.length > MAX_TEXT ? `${s.slice(0, MAX_TEXT)}…` : s
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null
}

/** 参数摘要：对象 JSON 序列化，字符串原样，空值为 '' */
function argsText(input: unknown): string {
  if (input == null) return ''
  return clip(typeof input === 'string' ? input : JSON.stringify(input))
}

/** 工具结果文本：字符串原样，对象 JSON 序列化 */
function resultText(result: ToolCall['result']): string {
  return clip(typeof result === 'string' ? result : JSON.stringify(result))
}

/** OpenAI arguments 为 JSON 字符串：解析后重新序列化（去掉转义），失败则原样展示 */
function openAiArgsText(args: unknown): string {
  if (typeof args !== 'string') return argsText(args)
  try {
    return clip(JSON.stringify(JSON.parse(args)))
  } catch {
    return clip(args)
  }
}

/** Anthropic 格式：{role:'assistant', content:[{type:'text'|'tool_use', ...}]} */
function parseAnthropic(content: unknown[]): AssistantTurn {
  const turn: AssistantTurn = { texts: [], calls: [] }
  for (const block of content) {
    if (!isRecord(block)) continue
    if (block.type === 'text' && typeof block.text === 'string' && block.text.trim()) {
      turn.texts.push(block.text)
    }
    if (block.type === 'tool_use') {
      turn.calls.push({ name: String(block.name ?? ''), argsText: argsText(block.input) })
    }
  }
  return turn
}

/** OpenAI 兼容格式：{choices:[{message:{content, tool_calls:[{function:{name, arguments}}]}}]} */
function parseOpenAi(choices: unknown[]): AssistantTurn | null {
  const first = choices[0]
  if (!isRecord(first) || !isRecord(first.message)) return null
  const msg = first.message
  const turn: AssistantTurn = { texts: [], calls: [] }
  if (typeof msg.content === 'string' && msg.content.trim()) turn.texts.push(msg.content)
  if (!Array.isArray(msg.tool_calls)) return turn
  for (const tc of msg.tool_calls) {
    if (!isRecord(tc) || !isRecord(tc.function)) continue
    turn.calls.push({
      name: String(tc.function.name ?? ''),
      argsText: openAiArgsText(tc.function.arguments),
    })
  }
  return turn
}

/** 解析单行 JSON 为一个 assistant 回合；非法 JSON 或两种格式都不匹配返回 null */
function parseTurn(line: string): AssistantTurn | null {
  let obj: unknown
  try {
    obj = JSON.parse(line)
  } catch {
    return null
  }
  if (!isRecord(obj)) return null
  if (obj.role === 'assistant' && Array.isArray(obj.content)) return parseAnthropic(obj.content)
  if (Array.isArray(obj.choices)) return parseOpenAi(obj.choices)
  return null
}

/** 审计工具调用 → assistant/tool_call 消息（llm_raw 缺失时的兜底，args 用审计口径） */
function toCallMessage(call: ToolCall): ConversationMessage {
  return {
    role: 'assistant',
    kind: 'tool_call',
    text: `${call.tool} ${argsText(call.args)}`.trim(),
    toolName: call.tool,
  }
}

/** 审计工具调用 → user/tool_result 消息（携带风控判定，deny 时前端渲染红色） */
function toResultMessage(call: ToolCall): ConversationMessage {
  return {
    role: 'user',
    kind: 'tool_result',
    text: resultText(call.result),
    toolName: call.tool,
    riskVerdict: call.risk_verdict || undefined,
    riskReason: call.risk_reason || undefined,
  }
}

/** 降级渲染：保留 llm_raw 原文（非空时），并保证工具调用链可见 */
function fallbackMessages(llmRaw: string, toolCalls: ToolCall[]): ConversationMessage[] {
  const msgs: ConversationMessage[] = []
  if (llmRaw.trim()) msgs.push({ role: 'assistant', kind: 'text', text: llmRaw })
  for (const call of toolCalls) msgs.push(toCallMessage(call), toResultMessage(call))
  return msgs
}

/**
 * 构建决策轮的完整对话消息流。
 * 每个 assistant 回合的 text/tool_call 之后，按 seq 顺序插入对应审计 tool_result；
 * llm_raw 被截断导致审计有剩余时，剩余调用追加在末尾；空输入返回空数组。
 */
export function buildConversation(llmRaw: string, toolCalls: ToolCall[]): ConversationMessage[] {
  const turns = llmRaw
    .split('\n')
    .map(parseTurn)
    .filter((t): t is AssistantTurn => t !== null)
  if (turns.length === 0) return fallbackMessages(llmRaw, toolCalls)
  const audit = [...toolCalls].sort((a, b) => a.seq - b.seq)
  const msgs: ConversationMessage[] = []
  let ai = 0 // 审计消费指针：回合内第 k 个 tool_use 对应 audit 顺序第 k 条
  for (const turn of turns) {
    for (const text of turn.texts) msgs.push({ role: 'assistant', kind: 'text', text })
    for (const call of turn.calls) {
      msgs.push({
        role: 'assistant',
        kind: 'tool_call',
        text: `${call.name} ${call.argsText}`.trim(),
        toolName: call.name,
      })
      if (ai < audit.length) msgs.push(toResultMessage(audit[ai++]))
    }
  }
  // llm_raw 缺尾部回合（如截断）：审计链剩余调用追加，保证工具链完整可见
  for (; ai < audit.length; ai++) msgs.push(toCallMessage(audit[ai]), toResultMessage(audit[ai]))
  return msgs
}
