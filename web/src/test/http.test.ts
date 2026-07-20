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
            margin: '47.86',
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

  it('getNotes：items[].created_at 映射为 time，round_id 透传（缺省空串）', async () => {
    vi.stubGlobal(
      'fetch',
      stubFetch({
        '/api/notes': {
          items: [
            { id: 1, round_id: 'r1', content: '笔记', created_at: 1784367450 },
            { id: 2, content: '无归属笔记', created_at: 1784367449 },
          ],
        },
      }),
    )
    const notes = await httpApi.getNotes()
    expect(notes).toHaveLength(2)
    expect(notes[0].content).toBe('笔记')
    expect(notes[0].round_id).toBe('r1')
    expect(new Date(notes[0].time).getTime()).toBe(1784367450000)
    expect(notes[1].round_id).toBe('') // 后端缺省 → 空串（无归属）
  })

  it('getNotes：乱序输入按 created_at 降序输出（回归：后端 recent_notes 最旧在前，消费侧契约=最新在前）', async () => {
    vi.stubGlobal(
      'fetch',
      stubFetch({
        // 后端 /api/notes 原样透传 recent_notes 的正序（最旧在前）
        '/api/notes': {
          items: [
            { id: 1, round_id: 'r1', content: '最旧', created_at: 100 },
            { id: 2, round_id: 'r2', content: '最新', created_at: 300 },
            { id: 3, round_id: 'r3', content: '中间', created_at: 200 },
          ],
        },
      }),
    )
    const notes = await httpApi.getNotes()
    expect(notes.map((n) => n.content)).toEqual(['最新', '中间', '最旧'])
  })

  it('getDailyStats：当日统计三键数字字符串 → number（风控口径：realized_pnl/orders_today/max_orders_per_day）', async () => {
    vi.stubGlobal(
      'fetch',
      stubFetch({
        '/api/daily_stats': { realized_pnl: '41.37', orders_today: '7', max_orders_per_day: '20' },
      }),
    )
    const stats = await httpApi.getDailyStats()
    expect(stats).toEqual({ realized_pnl: 41.37, orders_today: 7, max_orders_per_day: 20 })
    expect(typeof stats.realized_pnl).toBe('number')
    expect(typeof stats.orders_today).toBe('number')
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
