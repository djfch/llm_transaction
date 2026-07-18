/**
 * http 适配层测试：后端 Decimal 序列化为数字字符串时，getAccount/getPositions
 * 必须转成 number（回归：字符串进 fmtNum 会让主页整页崩溃白屏）。
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

describe('http 适配层（数字字符串 → number）', () => {
  it('getAccount：equity/available/unrealised_pnl 全部转 number', async () => {
    vi.stubGlobal(
      'fetch',
      stubFetch({
        '/api/account': { equity: '10842.36', available: '9000', unrealised_pnl: '0' },
      }),
    )
    const account = await httpApi.getAccount()
    expect(account.equity).toBe(10842.36)
    expect(account.available).toBe(9000)
    expect(account.unrealised_pnl).toBe(0)
    expect(typeof account.equity).toBe('number')
  })

  it('getPositions：全部数字字段转 number，contract 保持字符串', async () => {
    vi.stubGlobal(
      'fetch',
      stubFetch({
        '/api/positions': [
          {
            contract: 'BTC_USDT',
            size: '12',
            entry_price: '118320',
            mark_price: '119650.5',
            leverage: '3',
            unrealised_pnl: '159.6',
            liq_price: '82400',
          },
        ],
      }),
    )
    const [p] = await httpApi.getPositions()
    expect(p.contract).toBe('BTC_USDT')
    expect(p.mark_price).toBe(119650.5)
    expect(typeof p.size).toBe('number')
    expect(typeof p.liq_price).toBe('number')
  })

  it('getEquity：points[].t 映射为 time，equity 转 number', async () => {
    vi.stubGlobal(
      'fetch',
      stubFetch({
        '/api/equity': {
          initial_equity: 10000,
          baseline_source: 'paper_config',
          points: [{ t: 1784381252, equity: 10000 }],
        },
      }),
    )
    const points = await httpApi.getEquity()
    expect(points).toHaveLength(1)
    expect(points[0].equity).toBe(10000)
    expect(new Date(points[0].time).getTime()).toBe(1784381252000)
  })

  it('getNotes：items[].created_at 映射为 time', async () => {
    vi.stubGlobal(
      'fetch',
      stubFetch({
        '/api/notes': { items: [{ id: 1, round_id: 'r1', content: '笔记', created_at: 1784367449 }] },
      }),
    )
    const notes = await httpApi.getNotes()
    expect(notes).toHaveLength(1)
    expect(notes[0].content).toBe('笔记')
    expect(new Date(notes[0].time).getTime()).toBe(1784367449000)
  })

  it('getRounds：context_summary→summary、created_at→started_at、pnl_after 留空', async () => {
    vi.stubGlobal(
      'fetch',
      stubFetch({
        '/api/rounds?offset=0&limit=1': {
          offset: 0,
          limit: 1,
          items: [
            {
              round_id: 'r1',
              wake_source: 'timer:60min',
              context_summary: '权益 10000，持仓 0',
              created_at: 1784375288,
            },
          ],
        },
      }),
    )
    const rounds = await httpApi.getRounds(0, 1)
    expect(rounds[0].summary).toBe('权益 10000，持仓 0')
    expect(rounds[0].wake_source).toBe('timer:60min')
    expect(new Date(rounds[0].started_at).getTime()).toBe(1784375288000)
    expect(rounds[0].pnl_after).toBeUndefined()
  })
})
