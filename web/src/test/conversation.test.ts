/**
 * buildConversation 测试：Anthropic/OpenAI 兼容/OpenAI Responses 三种 llm_raw 格式的
 * 回合解析、工具结果按 seq 插入、非 JSON 与空输入降级、deny 结果携带风控信息。
 */
import { describe, expect, it } from 'vitest'
import type { ToolCall } from '../api/types'
import { buildConversation } from '../utils/conversation'

/** 审计工具调用夹具（args 从简，断言不依赖它） */
function auditCall(seq: number, tool: string, result: string, verdict = '', reason = ''): ToolCall {
  return { seq, tool, args: {}, risk_verdict: verdict, risk_reason: reason, result, duration_ms: 10 }
}

// Anthropic 原生格式：3 个 assistant 回合按 \n 连接（text+tool_use 交错，最终回合为纯文本结论）
const ANTHROPIC_RAW = [
  JSON.stringify({
    role: 'assistant',
    content: [
      { type: 'text', text: '先查账户与 K 线。' },
      { type: 'tool_use', id: 't1', name: 'get_account', input: {} },
      { type: 'tool_use', id: 't2', name: 'get_candlesticks', input: { contract: 'BTC_USDT', interval: '1h', limit: 20 } },
    ],
  }),
  JSON.stringify({
    role: 'assistant',
    content: [
      { type: 'text', text: '突破确认，开多 4 张。' },
      { type: 'tool_use', id: 't3', name: 'place_order', input: { contract: 'BTC_USDT', size: 4 } },
    ],
  }),
  JSON.stringify({
    role: 'assistant',
    content: [{ type: 'text', text: '已开多，30 分钟后复查。' }],
  }),
].join('\n')

// OpenAI 兼容格式：首回合 content 为 null + tool_calls（arguments 为 JSON 字符串），次回合纯文本
const OPENAI_RAW = [
  JSON.stringify({
    choices: [
      {
        message: {
          content: null,
          tool_calls: [
            { id: 'c1', function: { name: 'get_account', arguments: '{}' } },
            { id: 'c2', function: { name: 'place_order', arguments: '{"contract":"BTC_USDT","size":4}' } },
          ],
        },
      },
    ],
  }),
  JSON.stringify({ choices: [{ message: { content: '开多完成。', tool_calls: null } }] }),
].join('\n')

// OpenAI Responses 格式：顶层带 id/created_at/instructions 等元数据（不应进入对话），
// 首回合 reasoning（跳过）+ message + function_call，次回合纯文本结论
const RESPONSES_RAW = [
  JSON.stringify({
    id: 'resp_1',
    created_at: 1785604246.0,
    instructions: '# 策略书……',
    output: [
      { type: 'reasoning', summary: [] },
      {
        type: 'message',
        role: 'assistant',
        content: [{ type: 'output_text', text: '先查 K 线确认趋势。' }],
      },
      {
        type: 'function_call',
        name: 'get_candlesticks',
        arguments: '{"contract":"BTC_USDT","interval":"4h"}',
        call_id: 'call_1',
        id: 'fc_1',
      },
    ],
  }),
  JSON.stringify({
    id: 'resp_2',
    created_at: 1785604251.0,
    output: [
      {
        type: 'message',
        role: 'assistant',
        content: [{ type: 'output_text', text: '震荡市观望，设置预警后休眠。' }],
      },
    ],
  }),
].join('\n')

describe('buildConversation（完整对话构建）', () => {
  it('anthropic：text/tool_use 交错展开，工具结果按 seq 插在各自调用后，最终回合文本为结论', () => {
    const msgs = buildConversation(ANTHROPIC_RAW, [
      auditCall(1, 'get_account', 'equity=10842.36'),
      auditCall(2, 'get_candlesticks', '返回 20 根 K 线'),
      auditCall(3, 'place_order', '已成交 4 张', 'allow'),
    ])
    expect(msgs.map((m) => `${m.role}/${m.kind}`)).toEqual([
      'assistant/text',
      'assistant/tool_call',
      'user/tool_result',
      'assistant/tool_call',
      'user/tool_result',
      'assistant/text',
      'assistant/tool_call',
      'user/tool_result',
      'assistant/text',
    ])
    expect(msgs[0].text).toBe('先查账户与 K 线。')
    expect(msgs[1].toolName).toBe('get_account')
    expect(msgs[2].text).toBe('equity=10842.36')
    // tool_call 的 text 为「工具名 + 参数摘要」，args 以 llm_raw 为准
    expect(msgs[3].text).toBe('get_candlesticks {"contract":"BTC_USDT","interval":"1h","limit":20}')
    expect(msgs.at(-1)?.text).toBe('已开多，30 分钟后复查。')
    expect(msgs.at(-1)?.kind).toBe('text')
  })

  it('openai：content 为 null 时不产生文本，arguments JSON 字符串被解析为对象摘要', () => {
    const msgs = buildConversation(OPENAI_RAW, [
      auditCall(1, 'get_account', 'equity=10842.36'),
      auditCall(2, 'place_order', '已成交 4 张', 'allow'),
    ])
    expect(msgs.map((m) => `${m.role}/${m.kind}`)).toEqual([
      'assistant/tool_call',
      'user/tool_result',
      'assistant/tool_call',
      'user/tool_result',
      'assistant/text',
    ])
    // arguments 原样是带转义的 JSON 字符串，解析后应为紧凑对象文本
    expect(msgs[2].text).toBe('place_order {"contract":"BTC_USDT","size":4}')
    expect(msgs.at(-1)?.text).toBe('开多完成。')
  })

  it('responses：reasoning 跳过、output_text 提取、function_call 转调用，顶层元数据不进对话', () => {
    const msgs = buildConversation(RESPONSES_RAW, [
      auditCall(1, 'get_candlesticks', '返回 24 根 K 线'),
    ])
    expect(msgs.map((m) => `${m.role}/${m.kind}`)).toEqual([
      'assistant/text',
      'assistant/tool_call',
      'user/tool_result',
      'assistant/text',
    ])
    expect(msgs[0].text).toBe('先查 K 线确认趋势。')
    // arguments 为 JSON 字符串，解析为紧凑对象摘要
    expect(msgs[1].text).toBe('get_candlesticks {"contract":"BTC_USDT","interval":"4h"}')
    expect(msgs[2].text).toBe('返回 24 根 K 线')
    // 最终回合文本只取结论，不展示整段 Response JSON
    expect(msgs.at(-1)?.text).toBe('震荡市观望，设置预警后休眠。')
    expect(msgs.some((m) => m.text.includes('"instructions"'))).toBe(false)
    expect(msgs.some((m) => m.text.includes('resp_1'))).toBe(false)
  })

  it('非 JSON（mock provider 原文）+ 无工具调用：降级为单条原文 text', () => {
    expect(buildConversation('mock-raw-1', [])).toEqual([
      { role: 'assistant', kind: 'text', text: 'mock-raw-1' },
    ])
  })

  it('非 JSON + 有工具调用：原文之外仍渲染工具调用链', () => {
    const msgs = buildConversation('mock-raw-1', [auditCall(1, 'get_account', 'ok')])
    expect(msgs.map((m) => `${m.role}/${m.kind}`)).toEqual([
      'assistant/text',
      'assistant/tool_call',
      'user/tool_result',
    ])
  })

  it('空 llm_raw：无工具调用返回空数组；有工具调用仍渲染工具链（进行中轮的收尾态）', () => {
    expect(buildConversation('', [])).toEqual([])
    const msgs = buildConversation('', [auditCall(1, 'get_account', 'ok')])
    expect(msgs.map((m) => m.kind)).toEqual(['tool_call', 'tool_result'])
  })

  it('deny 的 tool_result 携带 risk_verdict/risk_reason', () => {
    const raw = JSON.stringify({
      role: 'assistant',
      content: [
        { type: 'tool_use', id: 't1', name: 'place_order', input: { contract: 'BTC_USDT', size: 20 } },
      ],
    })
    const msgs = buildConversation(raw, [
      auditCall(1, 'place_order', '风控拒绝，未下单', 'deny', '单仓超限'),
    ])
    const result = msgs.find((m) => m.kind === 'tool_result')
    expect(result?.riskVerdict).toBe('deny')
    expect(result?.riskReason).toBe('单仓超限')
    expect(result?.text).toBe('风控拒绝，未下单')
  })
})
