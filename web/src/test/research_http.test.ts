/** 当前逐标的研报 HTTP 适配与错误处理。 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError, httpApi } from '../api/http'

function stubFetch(routes: Record<string, unknown>) {
  return vi.fn(async (input: RequestInfo | URL) => {
    const path = String(input)
    const body = routes[path]
    if (body === undefined) throw new Error('未打桩的路径: ' + path)
    return new Response(JSON.stringify(body), { status: 200 })
  })
}

afterEach(() => vi.unstubAllGlobals())

const ASSET = {
  contract: 'BTC_USDT',
  direction: '偏多',
  confidence: '高',
  horizon: '3日',
  market_regime: '上涨趋势',
  technical_confirmation: '确认',
  basis_type: '混合',
  data_status: '完整',
}

const RAW_REPORT = {
  id: 7,
  report_type: 'asia_open',
  schema_version: 3,
  summary: 'BTC 结构获得宏观催化确认',
  cross_market_view: 'BTC 强于 ETH',
  global_risks: ['亚盘流动性偏薄'],
  asset_views: [ASSET],
  error: '',
  round_id: 'rs-round-7',
  research_prompt_md5: '0123456789abcdef0123456789abcdef',
  created_at: 1784595600,
}

const RAW_LINK = {
  id: 3,
  report_id: 7,
  chain: [
    { node: '美国 6 月 CPI 同比回落', kind: '事件', timeline_id: 1287 },
    { node: '风险资产偏好修复', kind: '标的结论' },
  ],
  confidence: '0.72',
  evidence: ['金十日历：CPI 公布值 3.0%'],
  status: 'concluded',
  topic: 'CPI',
  supersedes_id: null,
  created_at: 1784595600,
}

describe('研报端点适配', () => {
  it('列表只适配当前报告头与逐标的摘要', async () => {
    vi.stubGlobal(
      'fetch',
      stubFetch({ '/api/research/reports?offset=0&limit=5': { items: [RAW_REPORT], total: 1 } }),
    )
    const page = await httpApi.getResearchReports(0, 5)
    const report = page.items[0]
    expect(page.total).toBe(1)
    expect(report.schemaVersion).toBe(3)
    expect(report.summary).toBe('BTC 结构获得宏观催化确认')
    expect(report.globalRisks).toEqual(['亚盘流动性偏薄'])
    expect(report.researchPromptMd5).toBe('0123456789abcdef0123456789abcdef')
    expect(report.assetViews[0]).toMatchObject({
      contract: 'BTC_USDT',
      direction: '偏多',
      marketRegime: '上涨趋势',
      technicalConfirmation: '确认',
    })
    expect(new Date(report.time).getTime()).toBe(1784595600000)
  })

  it('详情展开逐标的证据、风险和研判', async () => {
    vi.stubGlobal(
      'fetch',
      stubFetch({
        '/api/research/reports/7': {
          ...RAW_REPORT,
          asset_views: [{
            ...ASSET,
            evidence: [
              { point: '放量增仓', source: '4h' },
              { point: 'ETF 净流入' },
            ],
            risks: ['资金费率偏高'],
            narrative: 'BTC 逐标的研判',
            created_at: 1784595600,
          }],
          causal_links: [RAW_LINK],
        },
      }),
    )
    const detail = await httpApi.getResearchReport(7)
    expect(detail.assetViews[0].evidence).toEqual(['放量增仓（4h）', 'ETF 净流入'])
    expect(detail.assetViews[0].risks).toEqual(['资金费率偏高'])
    expect(detail.assetViews[0].narrative).toBe('BTC 逐标的研判')
    expect(detail.causalLinks[0]).toMatchObject({
      id: 3,
      reportId: 7,
      confidence: 0.72,
      topic: 'CPI',
      status: 'concluded',
    })
  })

  it('因果链防御性解析会丢弃无法展示的节点', async () => {
    vi.stubGlobal(
      'fetch',
      stubFetch({
        '/api/research/reports/7': {
          ...RAW_REPORT,
          asset_views: [{
            ...ASSET,
            evidence: [],
            risks: [],
            narrative: '',
            created_at: 1784595600,
          }],
          causal_links: [{
            ...RAW_LINK,
            evidence: '["A","B"]',
            chain: ['不是对象', { kind: '事件' }, { node: '  有效节点  ', kind: '结论' }],
          }],
        },
      }),
    )
    const link = (await httpApi.getResearchReport(7)).causalLinks[0]
    expect(link.evidence).toEqual(['A', 'B'])
    expect(link.chain).toEqual([{ node: '有效节点', kind: '结论' }])
  })

  it('runResearch 发送报告类型和窗口，点火响应回显类型与窗口', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        expect(String(input)).toBe('/api/research/run')
        expect(init?.method).toBe('POST')
        expect(JSON.parse(String(init?.body))).toEqual({ report_type: 'asia_open', hours: 8 })
        return new Response(JSON.stringify({
          started: true,
          report_type: 'asia_open',
          hours: 8,
        }))
      }),
    )
    const result = await httpApi.runResearch('asia_open', 8)
    // 点火契约：仅 started + 回显参数，不含执行结果
    expect(result).toMatchObject({
      started: true,
      reportType: 'asia_open',
      hours: 8,
    })
  })

  it('调度状态接口原样返回下一次 UTC+8 执行与日历状态', async () => {
    const raw = {
      enabled: true,
      items: [{ id: 'asia_open', kind: 'market_open', enabled: true, next_run_at: 1788201000 }],
      calendar: { state: 'fallback', last_refreshed_at: 1788000000, errors: {}, warning: '日历降级' },
    }
    vi.stubGlobal('fetch', stubFetch({ '/api/research/schedule-status': raw }))
    await expect(httpApi.getResearchScheduleStatus()).resolves.toEqual(raw)
  })

  for (const [status, detail] of [[409, '研报生成中'], [503, 'LLM 未配置'], [422, 'hours 越界']] as const) {
    it('runResearch 错误状态 ' + status + ' 透传 detail', async () => {
      vi.stubGlobal(
        'fetch',
        vi.fn(async () => new Response(JSON.stringify({ detail }), { status })),
      )
      const error: unknown = await httpApi.runResearch().catch((item: unknown) => item)
      expect(error).toBeInstanceOf(ApiError)
      expect((error as ApiError).status).toBe(status)
      expect((error as ApiError).detail).toBe(detail)
    })
  }
})

describe('getResearchLive 适配', () => {
  it('研报轮与工具调用原样透传', async () => {
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
          tool_calls: [{
            seq: 1,
            tool: 'get_research_market_data',
            args: { contract: 'BTC_USDT' },
            risk_verdict: '',
            risk_reason: '',
            result: { text: '概览' },
            duration_ms: 12,
          }],
        },
      }),
    )
    const live = await httpApi.getResearchLive()
    expect(live.round?.round_id).toBe('rs-1')
    expect(live.tool_calls[0].tool).toBe('get_research_market_data')
  })

  it('无研报轮返回空状态', async () => {
    vi.stubGlobal('fetch', stubFetch({ '/api/research/live': { round: null, tool_calls: [] } }))
    expect(await httpApi.getResearchLive()).toEqual({ round: null, tool_calls: [] })
  })

  it('带 roundId 参数：URL 拼 ?round_id= 查询串（pinned 按绑定 ID 直查）', async () => {
    const fetchMock = stubFetch({ '/api/research/live?round_id=rs-1': { round: null, tool_calls: [] } })
    vi.stubGlobal('fetch', fetchMock)
    // stub 按完整路径精确匹配：query 拼错会直接抛「未打桩的路径」
    await expect(httpApi.getResearchLive('rs-1')).resolves.toEqual({ round: null, tool_calls: [] })
    expect(String(fetchMock.mock.calls[0][0])).toContain('round_id=rs-1')
  })
})
