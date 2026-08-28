/**
 * 工具执行结果拆包：后端三类 agent（交易/复盘/研报）统一把结果存为 {"text": 结果字符串}，
 * 展示层只应呈现 text 的值（\n 为真实换行），不把 JSON 外壳暴露给用户。
 */
import type { ToolCall } from '../api/types'

/**
 * 取工具执行结果的展示文本。
 * string 原样返回（历史兼容）；{text: string} 取值（主路径）；其他对象 JSON 序列化兜底。
 */
export function toolResultText(result: ToolCall['result']): string {
  if (typeof result === 'string') return result
  if (result !== null && typeof result === 'object' && typeof result.text === 'string') {
    return result.text
  }
  return JSON.stringify(result)
}
