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

/** 后端复盘报告原始项（契约 10 键） */
const RAW_REPORT = {
  id: 7,
  period_start: 1784505600,
  period_end: 1784592000,
  stats_json: '{"close_count":3,"total_pnl":"-32.10"}',
  report_md: '# 复盘报告\n\n本区间亏损。',
  strategy_action: 'rewrite',
  new_version_id: 3,
  error: '',
  round_id: 'rvw-round-7',
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
  it('getReviewReports：10 键 snake→camel、Unix 秒→ISO、statsJson 保留原文、total 透传', async () => {
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
    expect(r.roundId).toBe('rvw-round-7')
    expect(new Date(r.time).getTime()).toBe(1784595600000)
  })

  it('getReviewReports：raw 缺 round_id 字段（功能上线前的老数据）时 roundId 降级为空串', async () => {
    const legacy: Record<string, unknown> = { ...RAW_REPORT }
    delete legacy.round_id
    vi.stubGlobal(
      'fetch',
      stubFetch({ '/api/review/reports?offset=0&limit=5': { items: [legacy], total: 1 } }),
    )
    const [r] = (await httpApi.getReviewReports(0, 5)).items
    expect(r.roundId).toBe('')
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

  it('getReviewReport：详情同 10 键（reportMd 全文）', async () => {
    vi.stubGlobal('fetch', stubFetch({ '/api/review/reports/7': RAW_REPORT }))
    const r = await httpApi.getReviewReport(7)
    expect(r.id).toBe(7)
    expect(r.reportMd).toContain('本区间亏损。')
    expect(r.roundId).toBe('rvw-round-7')
  })

  it('runReview：点火响应 started + 统计区间回显，snake 键转 camelCase', async () => {
    vi.stubGlobal(
      'fetch',
      stubFetch({
        '/api/review/run': {
          started: true,
          period_start: 1784505600,
          period_end: 1784592000,
        },
      }),
    )
    const r = await httpApi.runReview()
    // 点火契约：仅 started + 区间回显，不含执行结果
    expect(r.started).toBe(true)
    expect(r.periodStart).toBe(1784505600)
    expect(r.periodEnd).toBe(1784592000)
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

  it('getRound：保留首次 USER 上下文；历史缺省字段降级为空串', async () => {
    vi.stubGlobal(
      'fetch',
      stubFetch({
        '/api/rounds/r1': {
          round_id: 'r1',
          prompt_snapshot: 'prompt',
          context_snapshot: '首次 USER 上下文',
          llm_raw: 'raw',
          tool_calls: [],
          strategy_md5: 'md5-bbb',
        },
        '/api/rounds/r2': { round_id: 'r2', prompt_snapshot: 'prompt', llm_raw: 'raw', tool_calls: [] },
      }),
    )
    const current = await httpApi.getRound('r1')
    expect(current.strategyMd5).toBe('md5-bbb')
    expect(current.context_snapshot).toBe('首次 USER 上下文')
    const legacy = await httpApi.getRound('r2')
    expect(legacy.strategyMd5).toBe('')
    expect(legacy.context_snapshot).toBe('')
  })
})

describe('getReviewLive 适配', () => {
  it('round 非 null：snake_case 原样透传（与 getAgentLive 同约定），args/result 为已解析对象', async () => {
    vi.stubGlobal(
      'fetch',
      stubFetch({
        '/api/review/live': {
          round: {
            round_id: 'rv-1',
            wake_source: 'review',
            prompt_md5: 'md5',
            prompt_snapshot: 'prompt',
            context_snapshot: 'ctx',
            llm_raw: '',
            strategy_md5: 's-md5',
            started_at: 1784600000,
            ended_at: null,
            error: '',
          },
          tool_calls: [
            {
              seq: 1,
              tool: 'get_review_stats',
              args: { interval_days: 1 },
              risk_verdict: '',
              risk_reason: '',
              result: { text: '概览' },
              duration_ms: 12,
            },
          ],
        },
      }),
    )
    const live = await httpApi.getReviewLive()
    expect(live.round?.round_id).toBe('rv-1')
    expect(live.round?.wake_source).toBe('review')
    expect(live.round?.strategy_md5).toBe('s-md5')
    expect(live.round?.started_at).toBe(1784600000)
    expect(live.round?.ended_at).toBeNull()
    expect(live.tool_calls).toHaveLength(1)
    expect(live.tool_calls[0].tool).toBe('get_review_stats')
    expect(live.tool_calls[0].args).toEqual({ interval_days: 1 })
    expect(live.tool_calls[0].result).toEqual({ text: '概览' }) // 后端复盘工具结果一律 {text} 包装
  })

  it('无复盘轮：round 为 null、tool_calls 为空数组', async () => {
    vi.stubGlobal('fetch', stubFetch({ '/api/review/live': { round: null, tool_calls: [] } }))
    const live = await httpApi.getReviewLive()
    expect(live.round).toBeNull()
    expect(live.tool_calls).toEqual([])
  })

  it('带 roundId 参数：URL 拼 ?round_id= 查询串（pinned 按绑定 ID 直查）', async () => {
    const fetchMock = stubFetch({ '/api/review/live?round_id=rv-1': { round: null, tool_calls: [] } })
    vi.stubGlobal('fetch', fetchMock)
    // stub 按完整路径精确匹配：query 拼错会直接抛「未打桩的路径」
    await expect(httpApi.getReviewLive('rv-1')).resolves.toEqual({ round: null, tool_calls: [] })
    expect(String(fetchMock.mock.calls[0][0])).toContain('round_id=rv-1')
  })
})
