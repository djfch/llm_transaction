/**
 * format 工具防御测试：undefined/NaN 输入显示 '-'（回归：fmtNum(undefined) 曾让主页白屏）。
 */
import { describe, expect, it } from 'vitest'
import { fmtNum, fmtPrice, fmtSigned } from '../utils/format'

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
