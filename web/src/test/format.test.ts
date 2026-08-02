/**
 * format 工具防御测试：undefined/NaN 输入统一显示 '-'。
 */
import { describe, expect, it } from 'vitest'
import { fmtNum, fmtPrice, fmtSigned, shortRoundId } from '../utils/format'

describe('shortRoundId(决策轮短号)', () => {
  it('uuid 取前 8 位；含分隔符取末段；空串返回空串', () => {
    expect(shortRoundId('a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6')).toBe('a1b2c3d4')
    expect(shortRoundId('round-0037')).toBe('0037')
    expect(shortRoundId('')).toBe('')
  })
})

describe('format 空值防御', () => {
  it('fmtNum：undefined/NaN 显示 -，正常值千分位', () => {
    expect(fmtNum(undefined as unknown as number)).toBe('-')
    expect(fmtNum(NaN)).toBe('-')
    expect(fmtNum(10842.36)).toBe('10,842.36')
  })

  it('fmtSigned：undefined 显示 -，正值带 +', () => {
    expect(fmtSigned(undefined as unknown as number)).toBe('-')
    expect(fmtSigned(159.6)).toBe('+159.60')
  })

  it('fmtPrice：undefined 显示 -，小数保留精度', () => {
    expect(fmtPrice(undefined as unknown as number)).toBe('-')
    expect(fmtPrice(0.07249)).toBe('0.07249')
  })
})
