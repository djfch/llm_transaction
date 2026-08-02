/**
 * 复盘/策略版本端点 http 适配测试：
 * snake_case → camelCase、created_at(Unix秒) → ISO、stats_json 保留原文、
 * diff 纯文本透传（不经 JSON 解析）、409/404/422 经 ApiError.detail 抛出；
 * 同时覆盖 /api/rounds(+{id}) 的 strategy_md5 → strategyMd5 适配。
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError, httpApi } from '../api/http'

/** 构造一个按路径返回固定 JSON 的假 fetch（同 http.test.ts 模式） */
function stubFetch(routes: Record<string, unknown>) {
  return vi.fn(async (input: RequestInfo | URL) => {
    const path = String(input)
    const body = routes[path]
    if (body === undefined) throw new Error(`未打桩的路径: ${path}`)
    return new Response(JSON.stringify(body), { status: 200 })
  })
}

afterEach(() => vi.unstubAllGlobals())

/** 后端复盘报告原始项（契约 9 键） */
const RAW_REPORT = {
  id: 7,
  period_start: 1784505600,
  period_end: 1784592000,
  stats_json: '{"close_count":3,"total_pnl":"-32.10"}',
  report_md: '# 复盘报告\n\n本区间亏损。',
  strategy_action: 'rewrite',
  new_version_id: 3,
  error: '',
  created_at: 1784595600,
}

/** 后端策略版本原始项（列表无 content，详情有） */
const RAW_VERSION = {
  id: 2,
  md5: 'b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7',
  created_by: 'review_agent',
  reason: '复盘改写',
  report_id: 1,
  created_at: 1784500000,
}

describe('复盘端点适配', () => {
  it('getReviewReports：9 键 snake→camel、Unix 秒→ISO、statsJson 保留原文、total 透传', async () => {
    vi.stubGlobal(
      'fetch',
      stubFetch({ '/api/review/reports?offset=0&limit=5': { items: [RAW_REPORT], total: 9 } }),
    )
    const page = await httpApi.getReviewReports(0, 5)
    expect(page.total).toBe(9)
    const r = page.items[0]
    expect(r.id).toBe(7)
    expect(new Date(r.periodStart).getTime()).toBe(1784505600000)
    expect(new Date(r.periodEnd).getTime()).toBe(1784592000000)
    expect(r.statsJson).toBe('{"close_count":3,"total_pnl":"-32.10"}')
    expect(r.reportMd).toBe('# 复盘报告\n\n本区间亏损。')
    expect(r.strategyAction).toBe('rewrite')
    expect(r.newVersionId).toBe(3)
    expect(r.error).toBe('')
    expect(new Date(r.time).getTime()).toBe(1784595600000)
  })

  it('getReviewReports：strategy_action 非 rewrite 归一为 none，new_version_id null 透传', async () => {
    vi.stubGlobal(
      'fetch',
      stubFetch({
        '/api/review/reports?offset=0&limit=5': {
          items: [{ ...RAW_REPORT, strategy_action: 'none', new_version_id: null }],
          total: 1,
        },
      }),
    )
    const [r] = (await httpApi.getReviewReports(0, 5)).items
    expect(r.strategyAction).toBe('none')
    expect(r.newVersionId).toBeNull()
  })

  it('getReviewReport：详情同 9 键（reportMd 全文）', async () => {
    vi.stubGlobal('fetch', stubFetch({ '/api/review/reports/7': RAW_REPORT }))
    const r = await httpApi.getReviewReport(7)
    expect(r.id).toBe(7)
    expect(r.reportMd).toContain('本区间亏损。')
  })

  it('runReview：started/ok 保留，snake 键转 camelCase', async () => {
    vi.stubGlobal(
      'fetch',
      stubFetch({
        '/api/review/run': {
          started: true,
          ok: true,
          report_id: 8,
          round_id: 'rv-round',
          strategy_action: 'none',
          new_version_id: null,
        },
      }),
    )
    const r = await httpApi.runReview()
    expect(r.started).toBe(true)
    expect(r.ok).toBe(true)
    expect(r.reportId).toBe(8)
    expect(r.roundId).toBe('rv-round')
    expect(r.newVersionId).toBeNull()
  })

  it('runReview：409 复盘进行中 → ApiError 带 detail（message 同 detail）', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response(JSON.stringify({ detail: '复盘进行中' }), { status: 409 })),
    )
    const err: unknown = await httpApi.runReview().catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).status).toBe(409)
    expect((err as ApiError).detail).toBe('复盘进行中')
    expect((err as Error).message).toBe('复盘进行中')
  })

  it('runReview：503 LLM 未配置 → ApiError 带 detail', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response(JSON.stringify({ detail: 'LLM 未配置' }), { status: 503 })),
    )
    const err: unknown = await httpApi.runReview().catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).status).toBe(503)
    expect((err as ApiError).detail).toBe('LLM 未配置')
  })
})

describe('策略版本端点适配', () => {
  it('getStrategyVersions：取 items 数组、created_by/report_id 转 camelCase、created_at→ISO、无 content', async () => {
    vi.stubGlobal(
      'fetch',
      stubFetch({
        '/api/strategy/versions': {
          items: [RAW_VERSION, { ...RAW_VERSION, id: 1, created_by: 'human', report_id: null }],
        },
      }),
    )
    const list = await httpApi.getStrategyVersions()
    expect(list).toHaveLength(2)
    expect(list[0].createdBy).toBe('review_agent')
    expect(list[0].reportId).toBe(1)
    expect(new Date(list[0].time).getTime()).toBe(1784500000000)
    expect(list[1].createdBy).toBe('human')
    expect(list[1].reportId).toBeNull()
    expect('content' in list[0]).toBe(false)
  })

  it('getStrategyVersion：详情含 content 全文', async () => {
    vi.stubGlobal(
      'fetch',
      stubFetch({ '/api/strategy/versions/2': { ...RAW_VERSION, content: '策略书全文' } }),
    )
    const v = await httpApi.getStrategyVersion(2)
    expect(v.id).toBe(2)
    expect(v.content).toBe('策略书全文')
    expect(v.createdBy).toBe('review_agent')
  })

  it('getStrategyDiff：PlainText 原文透传（不经 JSON 解析），query 为 from/to', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        expect(String(input)).toBe('/api/strategy/diff?from=1&to=2')
        return new Response('--- v1\n+++ v2\n@@ -1 +1 @@\n-旧策略\n+新策略', { status: 200 })
      }),
    )
    const text = await httpApi.getStrategyDiff(1, 2)
    expect(text).toContain('--- v1')
    expect(text).toContain('-旧策略')
    expect(text).toContain('+新策略')
  })

  it('rollbackStrategy：rolled_back_to/version 转 camelCase', async () => {
    vi.stubGlobal('fetch', stubFetch({ '/api/strategy/rollback/2': { rolled_back_to: 2, version: 4 } }))
    const r = await httpApi.rollbackStrategy(2)
    expect(r).toEqual({ rolledBackTo: 2, version: 4 })
  })

  it('rollbackStrategy：404 版本不存在 → ApiError 带 detail', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response(JSON.stringify({ detail: '策略版本不存在: 99' }), { status: 404 })),
    )
    const err: unknown = await httpApi.rollbackStrategy(99).catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).status).toBe(404)
    expect((err as ApiError).detail).toBe('策略版本不存在: 99')
  })
})

describe('PUT /api/strategy 校验失败', () => {
  it('422 → ApiError.detail 携带「；」分隔的校验原因（策略编辑器据此展示）', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(
        async () =>
          new Response(JSON.stringify({ detail: 'strip 后不足 100 字符；与当前版本无差异' }), { status: 422 }),
      ),
    )
    const err: unknown = await httpApi.putStrategy('x').catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).status).toBe(422)
    expect((err as ApiError).detail).toContain('；')
  })
})

describe('rounds 的 strategy_md5 适配', () => {
  it('getRounds：items[].strategy_md5 → strategyMd5', async () => {
    vi.stubGlobal(
      'fetch',
      stubFetch({
        '/api/rounds?offset=0&limit=1': {
          offset: 0,
          limit: 1,
          total: 1,
          items: [
            {
              round_id: 'r1',
              wake_source: 'timer:60min',
              context_summary: '权益 10000，持仓 0',
              created_at: 1784375288,
              strategy_md5: 'md5-aaa',
            },
          ],
        },
      }),
    )
    const rounds = await httpApi.getRounds(0, 1)
    expect(rounds.items[0].strategyMd5).toBe('md5-aaa')
  })

  it('getRound：strategy_md5 → strategyMd5；历史数据缺省为 空串', async () => {
    vi.stubGlobal(
      'fetch',
      stubFetch({
        '/api/rounds/r1': {
          round_id: 'r1',
          prompt_snapshot: 'prompt',
          llm_raw: 'raw',
          tool_calls: [],
          strategy_md5: 'md5-bbb',
        },
        '/api/rounds/r2': { round_id: 'r2', prompt_snapshot: 'prompt', llm_raw: 'raw', tool_calls: [] },
      }),
    )
    expect((await httpApi.getRound('r1')).strategyMd5).toBe('md5-bbb')
    expect((await httpApi.getRound('r2')).strategyMd5).toBe('')
  })
})
