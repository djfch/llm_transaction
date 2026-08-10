import { afterEach, describe, expect, it, vi } from 'vitest'
import { httpApi } from '../api/http'

afterEach(() => vi.unstubAllGlobals())

describe('研报 v2 端点适配', () => {
  it('适配逐标的摘要、详情与手动响应 asset_count', async () => {
    const base = {
      id: 9,
      report_type: 'manual',
      error: '',
      round_id: 'r-v2',
      schema_version: 2,
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
            verify_result: '',
            created_at: 1784595600,
          }],
          causal_links: [],
        }))
      }
      if (path.endsWith('/research/run')) {
        return new Response(JSON.stringify({
          started: true, ok: true, report_id: 9, round_id: 'r-v2', asset_count: 1,
        }))
      }
      throw new Error('未打桩路径: ' + path)
    }))

    const listed = (await httpApi.getResearchReports(0, 5)).items[0]
    expect(listed.schemaVersion).toBe(2)
    expect(listed.assetViews?.[0].contract).toBe('BTC_USDT')
    const detail = await httpApi.getResearchReport(9)
    expect(detail.globalRisks).toEqual(['宏观波动'])
    expect(detail.assetViews?.[0].evidence).toEqual(['放量增仓（4h）'])
    expect(detail.assetViews?.[0].marketRegime).toBe('上涨趋势')
    expect((await httpApi.runResearch()).assetCount).toBe(1)
  })
})
