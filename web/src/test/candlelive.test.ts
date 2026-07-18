/**
 * K 线实时跳动纯函数（与图表库解耦，可单测）：
 * intervalToSec 周期→秒；barStart 当前 K 线开盘时刻；mergeTick 把一笔最新价合并进当前 bar。
 */
import { describe, expect, it } from 'vitest'
import { barStart, intervalToSec, mergeTick } from '../utils/candleLive'

describe('intervalToSec(周期→秒)', () => {
  it('Gate 全 15 周期映射正确', () => {
    expect(intervalToSec('10s')).toBe(10)
    expect(intervalToSec('1m')).toBe(60)
    expect(intervalToSec('5m')).toBe(300)
    expect(intervalToSec('15m')).toBe(900)
    expect(intervalToSec('30m')).toBe(1800)
    expect(intervalToSec('1h')).toBe(3600)
    expect(intervalToSec('2h')).toBe(7200)
    expect(intervalToSec('4h')).toBe(14400)
    expect(intervalToSec('6h')).toBe(21600)
    expect(intervalToSec('8h')).toBe(28800)
    expect(intervalToSec('12h')).toBe(43200)
    expect(intervalToSec('1d')).toBe(86400)
    expect(intervalToSec('7d')).toBe(604800)
    expect(intervalToSec('30d')).toBe(2592000)
    expect(intervalToSec('1w')).toBe(604800)
  })

  it('未知周期返回 null（调用方跳过实时更新）', () => {
    expect(intervalToSec('3h')).toBeNull()
  })
})

describe('barStart(当前K线开盘时刻)', () => {
  it('向下取整到周期边界', () => {
    expect(barStart(3690, 3600)).toBe(3600) // 1h 周期内
    expect(barStart(3600, 3600)).toBe(3600) // 恰好边界
    expect(barStart(95, 60)).toBe(60)
  })
})

describe('mergeTick(最新价合并进当前bar)', () => {
  const bar = { time: 3600, open: 100, high: 110, low: 90, close: 105 }

  it('无当前 bar（null）：以最新价开新 bar', () => {
    expect(mergeTick(null, 120, 3600)).toEqual({ time: 3600, open: 120, high: 120, low: 120, close: 120 })
  })

  it('barTime 更晚（进入下一周期）：以最新价开新 bar，不沿用旧高低', () => {
    expect(mergeTick(bar, 120, 7200)).toEqual({ time: 7200, open: 120, high: 120, low: 120, close: 120 })
  })

  it('barTime 相同：合并 high/low/close，open 与 time 不变', () => {
    expect(mergeTick(bar, 120, 3600)).toEqual({ time: 3600, open: 100, high: 120, low: 90, close: 120 })
    expect(mergeTick(bar, 80, 3600)).toEqual({ time: 3600, open: 100, high: 110, low: 80, close: 80 })
    expect(mergeTick(bar, 100, 3600)).toEqual({ time: 3600, open: 100, high: 110, low: 90, close: 100 })
  })

  it('barTime 早于当前 bar（迟到的旧周期 tick）：返回 null 忽略', () => {
    expect(mergeTick(bar, 120, 1800)).toBeNull()
  })
})
