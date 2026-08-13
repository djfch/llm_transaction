/**
 * 决策轮「完整对话」构建：把 llm_raw 解析为消息流，供对话视图渲染。
 *
 * 后端事实：llm_raw = 每个 assistant 回合的原生 API 响应 JSON（单行紧凑）按 \n 连接；
 * 工具执行结果不在 llm_raw 里，而在审计 tool_calls（按 seq 顺序与回合内 tool_use 依次对应）。
 * 支持 Anthropic 原生格式、OpenAI 兼容（chat.completions）与 OpenAI Responses（顶层
 * output 数组）三种格式；解析失败时降级为「原文 + 工具调用链」。
 */
import type { ToolCall } from '../api/types'

/** 对话视图消息：assistant 的文本/工具调用 + user 角色的工具结果 */
export interface ConversationMessage {
  role: 'assistant' | 'user'
  kind: 'reasoning' | 'text' | 'tool_call' | 'tool_result'
  text: string // text: 思考/结论文本；tool_call: 工具名+参数摘要；tool_result: 返回内容
  toolName?: string
  riskVerdict?: string // tool_result 上携带，'deny' 时前端渲染红色
  riskReason?: string
}

/** 单回合解析产物：若干文本块 + 若干工具调用（参数已序列化为摘要文本） */
interface AssistantTurn {
  blocks: Array<
    | { kind: 'reasoning' | 'text'; text: string }
    | { kind: 'tool_call'; name: string; argsText: string }
  >
}

/** 单个 LLM 回合：保留该回合的思考、回复、工具调用与对应工具返回。 */
export interface ConversationTurn {
  messages: ConversationMessage[]
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null
}

/** 参数摘要：对象 JSON 序列化，字符串原样，空值为 '' */
function argsText(input: unknown): string {
  if (input == null) return ''
  return typeof input === 'string' ? input : JSON.stringify(input)
}

/** 工具结果文本：字符串原样，对象 JSON 序列化 */
function resultText(result: ToolCall['result']): string {
  return typeof result === 'string' ? result : JSON.stringify(result)
}

/** OpenAI arguments 为 JSON 字符串：解析后重新序列化（去掉转义），失败则原样展示 */
function openAiArgsText(args: unknown): string {
  if (typeof args !== 'string') return argsText(args)
  try {
    return JSON.stringify(JSON.parse(args))
  } catch {
    return args
  }
}

/** Anthropic 格式：提取 thinking 明文；redacted_thinking/signature 不展示。 */
function parseAnthropic(content: unknown[]): AssistantTurn {
  const turn: AssistantTurn = { blocks: [] }
  for (const block of content) {
    if (!isRecord(block)) continue
    if (block.type === 'thinking' && typeof block.thinking === 'string' && block.thinking.trim()) {
      turn.blocks.push({ kind: 'reasoning', text: block.thinking })
    }
    if (block.type === 'text' && typeof block.text === 'string' && block.text.trim()) {
      turn.blocks.push({ kind: 'text', text: block.text })
    }
    if (block.type === 'tool_use') {
      turn.blocks.push({
        kind: 'tool_call',
        name: String(block.name ?? ''),
        argsText: argsText(block.input),
      })
    }
  }
  return turn
}

/** OpenAI 兼容格式：{choices:[{message:{content, tool_calls:[{function:{name, arguments}}]}}]} */
function parseOpenAi(choices: unknown[]): AssistantTurn | null {
  const first = choices[0]
  if (!isRecord(first) || !isRecord(first.message)) return null
  const msg = first.message
  const turn: AssistantTurn = { blocks: [] }
  if (typeof msg.reasoning_content === 'string' && msg.reasoning_content.trim()) {
    turn.blocks.push({ kind: 'reasoning', text: msg.reasoning_content })
  }
  if (typeof msg.content === 'string' && msg.content.trim()) {
    turn.blocks.push({ kind: 'text', text: msg.content })
  }
  if (!Array.isArray(msg.tool_calls)) return turn
  for (const tc of msg.tool_calls) {
    if (!isRecord(tc) || !isRecord(tc.function)) continue
    turn.blocks.push({
      kind: 'tool_call',
      name: String(tc.function.name ?? ''),
      argsText: openAiArgsText(tc.function.arguments),
    })
  }
  return turn
}

/** OpenAI Responses 格式：reasoning 只提取 summary_text，encrypted_content 不展示。 */
function parseResponses(output: unknown[]): AssistantTurn {
  const turn: AssistantTurn = { blocks: [] }
  for (const item of output) {
    if (!isRecord(item)) continue
    if (item.type === 'reasoning' && Array.isArray(item.summary)) {
      for (const summary of item.summary) {
        if (
          isRecord(summary) &&
          summary.type === 'summary_text' &&
          typeof summary.text === 'string' &&
          summary.text.trim()
        ) {
          turn.blocks.push({ kind: 'reasoning', text: summary.text })
        }
      }
    }
    if (item.type === 'message' && Array.isArray(item.content)) {
      for (const c of item.content) {
        if (isRecord(c) && c.type === 'output_text' && typeof c.text === 'string' && c.text.trim()) {
          turn.blocks.push({ kind: 'text', text: c.text })
        }
      }
    }
    if (item.type === 'function_call') {
      turn.blocks.push({
        kind: 'tool_call',
        name: String(item.name ?? ''),
        argsText: openAiArgsText(item.arguments),
      })
    }
  }
  return turn
}

/** 解析单行 JSON 为一个 assistant 回合；非法 JSON 或三种格式都不匹配返回 null */
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
  if (Array.isArray(obj.output)) return parseResponses(obj.output)
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
  if (llmRaw.trim()) msgs.push({ role: 'assistant', kind: 'text', text: safeFallbackText(llmRaw) })
  for (const call of toolCalls) msgs.push(toCallMessage(call), toResultMessage(call))
  return msgs
}

/**
 * 构建决策轮的完整对话消息流。
 * 每个 assistant 回合的 text/tool_call 之后，按 seq 顺序插入对应审计 tool_result；
 * llm_raw 被截断导致审计有剩余时，剩余调用追加在末尾；空输入返回空数组。
 */
export function buildConversation(llmRaw: string, toolCalls: ToolCall[]): ConversationMessage[] {
  return buildConversationTurns(llmRaw, toolCalls).flatMap((turn) => turn.messages)
}

/** 未识别原始格式的安全降级：保留普通内容，递归移除签名、密文与脱敏思考块。 */
function safeFallbackText(raw: string): string {
  return raw
    .split('\n')
    .map((line) => {
      try {
        return JSON.stringify(stripSensitive(JSON.parse(line)))
      } catch {
        return /signature|encrypted_content|redacted_thinking/i.test(line)
          ? '（原始响应含不可展示的签名或加密思考）'
          : line
      }
    })
    .join('\n')
}

/** 递归清理未知 JSON 中不可展示的供应商私有推理字段。 */
function stripSensitive(value: unknown): unknown {
  if (Array.isArray(value)) {
    return value
      .filter((item) => !(isRecord(item) && item.type === 'redacted_thinking'))
      .map(stripSensitive)
  }
  if (!isRecord(value)) return value
  if (value.type === 'redacted_thinking') return null
  return Object.fromEntries(
    Object.entries(value)
      .filter(([key]) => key !== 'signature' && key !== 'encrypted_content')
      .map(([key, child]) => [key, stripSensitive(child)]),
  )
}

/** 构建按 LLM 响应回合分组的完整对话，供界面逐回合折叠。 */
export function buildConversationTurns(llmRaw: string, toolCalls: ToolCall[]): ConversationTurn[] {
  const turns = llmRaw
    .split('\n')
    .map(parseTurn)
    .filter((t): t is AssistantTurn => t !== null)
  if (turns.length === 0) {
    const messages = fallbackMessages(llmRaw, toolCalls)
    return messages.length === 0 ? [] : [{ messages }]
  }
  const audit = [...toolCalls].sort((a, b) => a.seq - b.seq)
  const result: ConversationTurn[] = []
  let ai = 0 // 审计消费指针：回合内第 k 个 tool_use 对应 audit 顺序第 k 条
  for (const turn of turns) {
    const msgs: ConversationMessage[] = []
    const results: ConversationMessage[] = []
    for (const block of turn.blocks) {
      if (block.kind !== 'tool_call') {
        msgs.push({ role: 'assistant', kind: block.kind, text: block.text })
        continue
      }
      msgs.push({
        role: 'assistant',
        kind: 'tool_call',
        text: `${block.name} ${block.argsText}`.trim(),
        toolName: block.name,
      })
      if (ai < audit.length) results.push(toResultMessage(audit[ai++]))
    }
    msgs.push(...results)
    if (msgs.length > 0) result.push({ messages: msgs })
  }
  // llm_raw 缺尾部回合（如截断）：审计链剩余调用追加，保证工具链完整可见
  const remaining: ConversationMessage[] = []
  for (; ai < audit.length; ai++) {
    remaining.push(toCallMessage(audit[ai]), toResultMessage(audit[ai]))
  }
  if (remaining.length > 0) result.push({ messages: remaining })
  return result
}
