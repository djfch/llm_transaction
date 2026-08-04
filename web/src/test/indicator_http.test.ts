/**
 * http 适配层测试（指标）：getIndicatorConfig 的 kind 归一化与 fields 缺省补空；
 * getIndicatorSeries 的数字字符串 → number、null 保持、time 保持 Unix 秒、keys 逗号拼接。
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { httpApi } from '../api/http'

/** 构造一个按路径返回固定 JSON 的假 fetch */
function stubFetch(routes: Record<string, unknown>) {
  return vi.fn(async (input: RequestInfo | URL) => {
    const path = String(input)
    const body = routes[path]
    if (body === undefined) throw new Error(`未打桩的路径: ${path}`)
    return new Response(JSON.stringify(body), { status: 200 })
  })
}

afterEach(() => vi.unstubAllGlobals())

describe('http 适配层（指标）', () => {
  it('getIndicatorConfig：shortlist/available 透传，kind 归一化，fields 缺省补空数组', async () => {
    vi.stubGlobal(
      'fetch',
      stubFetch({
        '/api/indicator_config': {
          shortlist: ['ema20', 'rsi14', 'oi'],
          available: [
            { key: 'ema20', label: 'EMA20（指数均线）', kind: 'overlay', fields: ['ema20'] },
            { key: 'rsi14', label: 'RSI14（相对强弱指标）', kind: 'pane' }, // fields 缺省
            { key: 'mystery', label: '未知指标', kind: 'weird_kind', fields: ['x'] }, // 未知 kind 降级 pane
            { key: 'oi', label: 'OI（持仓量）', kind: 'scalar', fields: [] },
          ],
        },
      }),
    )
    const config = await httpApi.getIndicatorConfig()
    expect(config.shortlist).toEqual(['ema20', 'rsi14', 'oi'])
    expect(config.available).toHaveLength(4)
    expect(config.available[0]).toEqual({ key: 'ema20', label: 'EMA20（指数均线）', kind: 'overlay', fields: ['ema20'] })
    expect(config.available[1].fields).toEqual([]) // 缺省补空
    expect(config.available[2].kind).toBe('pane') // 未知 kind 不污染主图
    expect(config.available[3].kind).toBe('scalar')
  })

  it('getIndicatorSeries：value 数字字符串→number、null 保持、time 保持 Unix 秒、keys 逗号拼接', async () => {
    const fetchStub = stubFetch({
      '/api/indicators/series?contract=BTC_USDT&interval=1h&limit=200&keys=ema20%2Cmacd': {
        contract: 'BTC_USDT',
        interval: '1h',
        series: {
          ema20: {
            label: 'EMA20（指数均线）',
            kind: 'overlay',
            fields: {
              ema20: [
                { time: 1754275200, value: '115000.50' },
                { time: 1754278800, value: null },
              ],
            },
          },
          macd: {
            label: 'MACD（指数平滑异同移动平均线）',
            kind: 'pane',
            fields: {
              dif: [{ time: 1754275200, value: '-12.3456' }],
              dea: [],
              hist: [{ time: 1754275200, value: '3.21' }],
            },
          },
          oi: { label: 'OI（持仓量）', kind: 'scalar', fields: {}, current: '123456' },
        },
      },
    })
    vi.stubGlobal('fetch', fetchStub)
    const resp = await httpApi.getIndicatorSeries('BTC_USDT', '1h', ['ema20', 'macd'])
    expect(resp.contract).toBe('BTC_USDT')
    // keys 以逗号拼接（URL 编码为 %2C），limit 缺省 200
    expect(fetchStub).toHaveBeenCalledWith(
      '/api/indicators/series?contract=BTC_USDT&interval=1h&limit=200&keys=ema20%2Cmacd',
      expect.objectContaining({ headers: expect.objectContaining({ 'Content-Type': 'application/json' }) }),
    )
    const ema = resp.series.ema20.fields.ema20
    expect(ema[0]).toEqual({ time: 1754275200, value: 115000.5 })
    expect(typeof ema[0].value).toBe('number')
    expect(ema[1].value).toBeNull()
    expect(resp.series.macd.fields.dif[0].value).toBe(-12.3456)
    expect(resp.series.macd.fields.dea).toEqual([])
    expect(resp.series.oi.current).toBe(123456)
    expect(resp.series.oi.kind).toBe('scalar')
  })
})
