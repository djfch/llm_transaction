import { afterEach, describe, expect, it, vi } from 'vitest'
import { httpApi } from '../api/http'

afterEach(() => vi.unstubAllGlobals())

describe('研报 v2 端点适配', () => {
  it('适配逐标的摘要、详情与手动点火响应', async () => {
    const base = {
      id: 9,
      report_type: 'manual',
      error: '',
      round_id: 'r-v2',
      schema_version: 3,
      summary: '市场分化',
      cross_market_view: 'BTC 强于 ETH',
      created_at: 1784595600,
      asset_views: [{
        contract: 'BTC_USDT',
        direction: '偏多',
        confidence: '高',
        horizon: '3日',
        market_regime: '上涨趋势',
        technical_confirmation: '确认',
        basis_type: '混合',
        data_status: '完整',
      }],
    }
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input)
      if (path.endsWith('/research/reports?offset=0&limit=5')) {
        return new Response(JSON.stringify({ items: [base], total: 1 }))
      }
      if (path.endsWith('/research/reports/9')) {
        return new Response(JSON.stringify({
          ...base,
          global_risks: ['宏观波动'],
          asset_views: [{
            ...base.asset_views[0],
            evidence: [{ point: '放量增仓', source: '4h' }],
            risks: ['费率偏高'],
            narrative: 'BTC 研判',
            created_at: 1784595600,
          }],
          causal_links: [],
        }))
      }
      if (path.endsWith('/research/run')) {
        return new Response(JSON.stringify({
          started: true, report_type: 'manual', hours: 24,
        }))
      }
      throw new Error('未打桩路径: ' + path)
    }))

    const listed = (await httpApi.getResearchReports(0, 5)).items[0]
    expect(listed.schemaVersion).toBe(3)
    expect(listed.assetViews?.[0].contract).toBe('BTC_USDT')
    const detail = await httpApi.getResearchReport(9)
    expect(detail.globalRisks).toEqual(['宏观波动'])
    expect(detail.assetViews?.[0].evidence).toEqual(['放量增仓（4h）'])
    expect(detail.assetViews?.[0].marketRegime).toBe('上涨趋势')
    // 手动触发为点火契约：仅 started + 回显参数，不含 assetCount 等执行结果
    const run = await httpApi.runResearch()
    expect(run).toMatchObject({ started: true, reportType: 'manual', hours: 24 })
  })

  it('研报详情适配 research_reviews：evidence_reviews/outcome 解析为对象，created_at 转 ISO', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input)
      if (path.endsWith('/research/reports/9')) {
        return new Response(JSON.stringify({
          id: 9,
          report_type: 'asia_open',
          error: '',
          round_id: 'r-v2',
          schema_version: 3,
          summary: 's',
          cross_market_view: '',
          global_risks: [],
          created_at: 1784595600,
          asset_views: [{
            contract: 'BTC_USDT',
            direction: '偏多',
            confidence: '高',
            horizon: '24h',
            market_regime: '上涨趋势',
            technical_confirmation: '确认',
            basis_type: '混合',
            data_status: '完整',
            evidence: [],
            risks: [],
            narrative: '',
            created_at: 1784595600,
            research_reviews: [{
              id: 2,
              review_report_id: 7,
              direction_relation: 'realized',
              direction_reason: '方向一致',
              reasoning_quality: 'sound',
              reasoning_review: '因果链成立',
              evidence_reviews: [
                {
                  evidence_index: 0,
                  fact_status: 'confirmed',
                  reasoning_status: 'supported',
                  explanation: '引用准确',
                },
                { evidence_index: 'x' },
              ],
              confidence_assessment: 'appropriate',
              confidence_reason: '匹配',
              improvement_advice: '',
              outcome: { data_status: 'complete', start_price: 67400, end_price: 70800, return_pct: 5.04 },
              created_at: 1784595900,
              review_kind: 'manual',
              rereview_reason: '人工复核原结论',
            }, {
              // 旧契约缺省两新键：适配后 reviewKind 回退 auto、rereviewReason 空串（R5-2 兼容）
              id: 3,
              review_report_id: 7,
              direction_relation: 'diverged',
              reasoning_quality: 'partial',
              confidence_assessment: 'too_high',
              improvement_advice: '',
              created_at: 1784595901,
            }],
          }],
          causal_links: [],
        }))
      }
      throw new Error('未打桩路径: ' + path)
    }))

    const detail = await httpApi.getResearchReport(9)
    const review = detail.assetViews?.[0].researchReviews?.[0]
    expect(review?.reviewReportId).toBe(7)
    expect(review?.directionRelation).toBe('realized')
    expect(review?.directionReason).toBe('方向一致')
    expect(review?.reasoningReview).toBe('因果链成立')
    expect(review?.confidenceReason).toBe('匹配')
    // 结构非法的依据评价元素被丢弃（evidence_index 非数字）
    expect(review?.evidenceReviews).toEqual([{
      evidenceIndex: 0,
      factStatus: 'confirmed',
      reasoningStatus: 'supported',
      explanation: '引用准确',
    }])
    expect(review?.outcome).toMatchObject({ data_status: 'complete', return_pct: 5.04 })
    expect(review?.createdAt).toBe(new Date(1784595900 * 1000).toISOString())
    // R5-2：manual 两新键适配；旧契约缺省回退 auto/空串
    expect(review?.reviewKind).toBe('manual')
    expect(review?.rereviewReason).toBe('人工复核原结论')
    const legacy = detail.assetViews?.[0].researchReviews?.[1]
    expect(legacy?.reviewKind).toBe('auto')
    expect(legacy?.rereviewReason).toBe('')
  })

  it('requestResearchRereview 发送 snake_case 授权请求体并透出 id/reused；非 2xx 透传 detail', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input)
      if (path.endsWith('/review/research/rereview') && init?.method === 'POST') {
        expect(JSON.parse(String(init.body))).toEqual({
          report_id: 9,
          contract: 'BTC_USDT',
          reason: '原复盘误判',
        })
        return new Response(JSON.stringify({
          id: 5,
          report_id: 9,
          contract: 'BTC_USDT',
          reason: '原复盘误判',
          requested_by: 'human',
          created_at: 1784595900,
          consumed_round_id: '',
          reused: false,
        }))
      }
      throw new Error('未打桩路径: ' + path)
    }))
    const ack = await httpApi.requestResearchRereview(9, 'BTC_USDT', '原复盘误判')
    expect(ack).toMatchObject({ id: 5, reused: false })

    // 409（目标未被正式复盘）经 ApiError 透出 detail
    vi.stubGlobal('fetch', vi.fn(async () =>
      new Response(JSON.stringify({ detail: '该结论尚未被正式复盘，自动复盘路径会覆盖，无需授权重评' }), { status: 409 }),
    ))
    const error: unknown = await httpApi
      .requestResearchRereview(9, 'BTC_USDT', '复核')
      .catch((item: unknown) => item)
    expect(error).toMatchObject({ status: 409, detail: '该结论尚未被正式复盘，自动复盘路径会覆盖，无需授权重评' })
  })
})

describe('研报提示词端点适配', () => {
  it('GET/PUT 原文、版本列表/详情（created_at 转 ISO）、diff 查询参数与回滚结果', async () => {
    const versionRow = {
      id: 3,
      md5: 'md5-c',
      created_by: 'review_agent',
      reason: '复盘修订',
      review_report_id: 1,
      created_at: 1784595600,
      status: 'applied',
    }
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input)
      if (path.endsWith('/research/prompt') && init?.method === 'PUT') {
        return new Response(String(init.body))
      }
      if (path.endsWith('/research/prompt')) {
        return new Response('# 研报提示词原文')
      }
      if (path.endsWith('/research/prompt/versions')) {
        return new Response(JSON.stringify({ items: [versionRow] }))
      }
      if (path.endsWith('/research/prompt/versions/3')) {
        return new Response(JSON.stringify({ ...versionRow, content: '# v3 全文' }))
      }
      if (path.endsWith('/research/prompt/diff?from=1&to=3')) {
        return new Response('--- v1\n+++ v3\n-旧\n+新')
      }
      if (path.endsWith('/research/prompt/rollback/1') && init?.method === 'POST') {
        return new Response(JSON.stringify({ rolled_back_to: 1, version: 4 }))
      }
      throw new Error('未打桩路径: ' + path)
    }))

    expect(await httpApi.getResearchPrompt()).toBe('# 研报提示词原文')
    expect(await httpApi.putResearchPrompt('# 新文')).toBe('# 新文')

    const versions = await httpApi.getResearchPromptVersions()
    expect(versions).toHaveLength(1)
    expect(versions[0]).toMatchObject({ id: 3, createdBy: 'review_agent', reviewReportId: 1, status: 'applied' })
    expect(versions[0].time).toBe(new Date(1784595600 * 1000).toISOString())
    expect('content' in versions[0]).toBe(false)

    const detail = await httpApi.getResearchPromptVersion(3)
    expect(detail.content).toBe('# v3 全文')

    expect(await httpApi.getResearchPromptDiff(1, 3)).toContain('+++ v3')

    const rollback = await httpApi.rollbackResearchPrompt(1)
    expect(rollback).toEqual({ rolledBackTo: 1, version: 4 })
  })
})
