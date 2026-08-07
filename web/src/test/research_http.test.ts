/**
 * 研报端点 http 适配测试：
 * snake_case → camelCase、created_at(Unix秒) → ISO、evidence_json/risks_json/raw_json 保留原文、
 * 详情端 evidence/risks/raw 已解析 + 因果链防御性适配（JSON 字符串形态、缺 node 文本节点丢弃）、
 * runResearch 的 POST body 与 409/503/422 经 ApiError.detail 抛出、getResearchLive 原样透传。
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

/** 后端研报原始项（契约 13 键；时间为 created_at(Unix秒)） */
const RAW_REPORT = {
  id: 7,
  report_type: 'asia_open',
  direction: '偏多',
  confidence: '中',
  horizon: '24h',
  evidence_json: '["美国 6 月 CPI 同比 3.0%，低于预期 3.1%"]',
  risks_json: '["亚盘流动性偏薄"]',
  narrative: '亚盘时段宏观面偏多。',
  raw_json: '{"direction":"偏多","confidence":"中"}',
  verify_result: '',
  error: '',
  round_id: 'rs-round-7',
  created_at: 1784595600,
}

/** 后端因果链原始项（chain/evidence 契约上已解析为数组；kind 为中文自由文本；broken_at 为断点节点下标） */
const RAW_LINK = {
  id: 3,
  report_id: 7,
  chain: [
    { node: '美国 6 月 CPI 同比回落', kind: '事件', timeline_id: 1287 },
    { node: '风险资产偏好修复', kind: '标的结论' },
  ],
  confidence: '0.72', // 后端 Decimal 序列化为字符串
  evidence: ['金十日历：CPI 公布值 3.0%'],
  status: 'verified',
  broken_at: null,
  created_at: 1784595600,
}

describe('研报端点适配', () => {
  it('getResearchReports：13 键 snake→camel、Unix 秒→ISO、json 三字段保留原文、total 透传', async () => {
    vi.stubGlobal(
      'fetch',
      stubFetch({ '/api/research/reports?offset=0&limit=5': { items: [RAW_REPORT], total: 9 } }),
    )
    const page = await httpApi.getResearchReports(0, 5)
    expect(page.total).toBe(9)
    const r = page.items[0]
    expect(r.id).toBe(7)
    expect(r.reportType).toBe('asia_open')
    expect(r.direction).toBe('偏多')
    expect(r.confidence).toBe('中')
    expect(r.horizon).toBe('24h')
    expect(r.evidenceJson).toBe('["美国 6 月 CPI 同比 3.0%，低于预期 3.1%"]')
    expect(r.risksJson).toBe('["亚盘流动性偏薄"]')
    expect(r.rawJson).toBe('{"direction":"偏多","confidence":"中"}')
    expect(r.verifyResult).toBe('')
    expect(r.error).toBe('')
    expect(r.roundId).toBe('rs-round-7')
    expect(new Date(r.time).getTime()).toBe(1784595600000)
  })

  it('getResearchReports：raw 缺字段（老数据）时降级为空串/空 JSON', async () => {
    const legacy: Record<string, unknown> = { ...RAW_REPORT }
    delete legacy.round_id
    delete legacy.error
    delete legacy.evidence_json
    delete legacy.confidence
    vi.stubGlobal(
      'fetch',
      stubFetch({ '/api/research/reports?offset=0&limit=5': { items: [legacy], total: 1 } }),
    )
    const [r] = (await httpApi.getResearchReports(0, 5)).items
    expect(r.roundId).toBe('')
    expect(r.error).toBe('')
    expect(r.evidenceJson).toBe('[]')
    expect(r.confidence).toBe('')
  })

  it('getResearchReport：详情 13 键 + evidence/risks/raw 已解析 + 因果链适配', async () => {
    vi.stubGlobal(
      'fetch',
      stubFetch({
        '/api/research/reports/7': { ...RAW_REPORT, narrative: '（全文）', evidence: ['CPI 低于预期'], risks: ['流动性薄'], raw: { direction: '偏多' }, causal_links: [RAW_LINK] },
      }),
    )
    const d = await httpApi.getResearchReport(7)
    expect(d.id).toBe(7)
    expect(d.narrative).toBe('（全文）')
    expect(d.evidence).toEqual(['CPI 低于预期'])
    expect(d.risks).toEqual(['流动性薄'])
    expect(d.raw).toEqual({ direction: '偏多' })
    expect(d.causalLinks).toHaveLength(1)
    const link = d.causalLinks[0]
    expect(link.id).toBe(3)
    expect(link.reportId).toBe(7)
    expect(link.confidence).toBe(0.72) // 字符串 Decimal 转数值
    expect(link.evidence).toEqual(['金十日历：CPI 公布值 3.0%'])
    expect(link.status).toBe('verified')
    expect(link.brokenAt).toBeNull()
    expect(new Date(link.time).getTime()).toBe(1784595600000)
    expect(link.chain).toHaveLength(2)
    expect(link.chain[0]).toEqual({ node: '美国 6 月 CPI 同比回落', kind: '事件', timeline_id: 1287 })
    expect(link.chain[1]).toEqual({ node: '风险资产偏好修复', kind: '标的结论' }) // timeline_id 缺省不补
  })

  it('getResearchReport：evidence 契约对象数组 [{point,source}] → 「point（source）」展示串；字符串原样；混合各归各；risks 真字符串数组原样', async () => {
    vi.stubGlobal(
      'fetch',
      stubFetch({
        '/api/research/reports/7': {
          ...RAW_REPORT,
          evidence: [
            { point: '美国 6 月 CPI 同比 3.0%', source: '金十日历' },
            { point: 'BTC ETF 单日净流入', source: '律动快讯' },
            '字符串形态证据', // 兼容历史/防御
            { point: '仅有依据无来源' },
            { foo: '其他形状' }, // 非 {point,source} 形状兜底 String
          ],
          risks: ['风险点1', '风险点2'],
        },
      }),
    )
    const d = await httpApi.getResearchReport(7)
    expect(d.evidence).toEqual([
      '美国 6 月 CPI 同比 3.0%（金十日历）',
      'BTC ETF 单日净流入（律动快讯）',
      '字符串形态证据',
      '仅有依据无来源',
      '[object Object]',
    ])
    expect(d.risks).toEqual(['风险点1', '风险点2'])
  })

  it('getResearchReport：因果链防御——evidence 为 JSON 字符串形态时解析、chain 非数组降级空、缺 node 文本节点丢弃', async () => {
    vi.stubGlobal(
      'fetch',
      stubFetch({
        '/api/research/reports/7': {
          ...RAW_REPORT,
          evidence: '["字符串形态证据"]', // 防御兼容 JSON 字符串
          risks: 'not-a-list', // 非数组 → 空数组
          causal_links: [
            { ...RAW_LINK, evidence: '["A","B"]', chain: ['不是对象', { kind: '事件' }, { node: '  有效节点  ', kind: '标的结论', timeline_id: '1290' }] },
          ],
        },
      }),
    )
    const d = await httpApi.getResearchReport(7)
    expect(d.evidence).toEqual(['字符串形态证据'])
    expect(d.risks).toEqual([])
    const link = d.causalLinks[0]
    expect(link.evidence).toEqual(['A', 'B'])
    expect(link.chain).toHaveLength(1) // 非对象节点 + 缺 node 文本节点被丢弃
    expect(link.chain[0].node).toBe('有效节点')
    expect(link.chain[0].timeline_id).toBeUndefined() // 非数字 timeline_id 不保留
  })

  it('runResearch：POST body 为 report_type/hours，响应 snake 键转 camelCase（含 errorCode）', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        expect(String(input)).toBe('/api/research/run')
        expect(init?.method).toBe('POST')
        expect(JSON.parse(String(init?.body))).toEqual({ report_type: 'asia_open', hours: 8 })
        return new Response(
          JSON.stringify({
            started: true,
            ok: true,
            report_id: 8,
            round_id: 'rs-run',
            direction: '中性',
            confidence: '低',
            error: '',
            error_code: '',
          }),
          { status: 200 },
        )
      }),
    )
    const r = await httpApi.runResearch('asia_open', 8)
    expect(r.started).toBe(true)
    expect(r.ok).toBe(true)
    expect(r.reportId).toBe(8)
    expect(r.roundId).toBe('rs-run')
    expect(r.direction).toBe('中性')
    expect(r.confidence).toBe('低')
    expect(r.error).toBe('')
    expect(r.errorCode).toBe('')
  })

  it('runResearch：409 研报生成中 → ApiError 带 detail（message 同 detail）', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response(JSON.stringify({ detail: '研报生成中' }), { status: 409 })),
    )
    const err: unknown = await httpApi.runResearch().catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).status).toBe(409)
    expect((err as ApiError).detail).toBe('研报生成中')
    expect((err as Error).message).toBe('研报生成中')
  })

  it('runResearch：503 LLM 未配置 → ApiError 带 detail', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response(JSON.stringify({ detail: 'LLM 未配置' }), { status: 503 })),
    )
    const err: unknown = await httpApi.runResearch().catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).status).toBe(503)
    expect((err as ApiError).detail).toBe('LLM 未配置')
  })

  it('runResearch：422 hours 越界 → ApiError 带 detail', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response(JSON.stringify({ detail: 'hours 越界' }), { status: 422 })),
    )
    const err: unknown = await httpApi.runResearch('manual', 999).catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).status).toBe(422)
    expect((err as ApiError).detail).toBe('hours 越界')
  })
})

describe('getResearchLive 适配', () => {
  it('round 非 null：snake_case 原样透传（与 getAgentLive 同约定），args/result 为已解析对象', async () => {
    vi.stubGlobal(
      'fetch',
      stubFetch({
        '/api/research/live': {
          round: {
            round_id: 'rs-1',
            wake_source: 'research',
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
              tool: 'get_macro_context',
              args: { hours: 24 },
              risk_verdict: '',
              risk_reason: '',
              result: { text: '概览' },
              duration_ms: 12,
            },
          ],
        },
      }),
    )
    const live = await httpApi.getResearchLive()
    expect(live.round?.round_id).toBe('rs-1')
    expect(live.round?.wake_source).toBe('research')
    expect(live.round?.started_at).toBe(1784600000)
    expect(live.round?.ended_at).toBeNull()
    expect(live.tool_calls).toHaveLength(1)
    expect(live.tool_calls[0].tool).toBe('get_macro_context')
    expect(live.tool_calls[0].args).toEqual({ hours: 24 })
    expect(live.tool_calls[0].result).toEqual({ text: '概览' }) // 后端研报工具结果一律 {text} 包装
  })

  it('无研报轮：round 为 null、tool_calls 为空数组', async () => {
    vi.stubGlobal('fetch', stubFetch({ '/api/research/live': { round: null, tool_calls: [] } }))
    const live = await httpApi.getResearchLive()
    expect(live.round).toBeNull()
    expect(live.tool_calls).toEqual([])
  })
})
