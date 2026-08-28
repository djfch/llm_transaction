/** toolResultText 测试：{text} 拆包、string 历史兼容、其他对象 JSON 兜底 */
import { describe, expect, it } from 'vitest'
import { toolResultText } from '../utils/toolResult'

describe('toolResultText 工具结果拆包', () => {
  it('{text: string} 对象：返回 text 值，换行为真实换行符', () => {
    expect(toolResultText({ text: '第一行\n第二行' })).toBe('第一行\n第二行')
  })

  it('string（历史兼容）：原样返回', () => {
    expect(toolResultText('equity=10842.36')).toBe('equity=10842.36')
  })

  it('其他对象（无 text 字段）：JSON 序列化兜底，不丢信息', () => {
    expect(toolResultText({ equity: 10842.36 })).toBe('{"equity":10842.36}')
  })

  it('text 非 string 的对象：JSON 序列化兜底', () => {
    expect(toolResultText({ text: 42 })).toBe('{"text":42}')
  })
})
