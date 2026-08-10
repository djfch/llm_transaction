/** 研报 mock 与当前逐标的 API 契约一致性。 */
import { describe, expect, it } from 'vitest'
import { ApiError } from '../api/http'
import { mockApi } from '../api/mock'

describe('mock 初始研报', () => {
  it('列表包含一条失败报告和一条成功逐标的报告', async () => {
    const page = await mockApi.getResearchReports(0, 2)
    expect(page.total).toBe(2)
    expect(page.items[0].error).not.toBe('')
    expect(page.items[0].assetViews).toEqual([])
    expect(page.items[1].schemaVersion).toBe(2)
    expect(page.items[1].assetViews[0].contract).toBe('BTC_USDT')
    expect(['确认', '冲突', '中性', '不可用']).toContain(
      page.items[1].assetViews[0].technicalConfirmation,
    )
  })

  it('成功详情包含逐标的研判和版本化因果链', async () => {
    const detail = await mockApi.getResearchReport(1)
    const asset = detail.assetViews[0]
    expect(asset.narrative).toContain('亚盘时段宏观面偏多')
    expect(asset.evidence[0]).toBe('美国 6 月 CPI 同比 3.0%，低于预期 3.1%（金十日历）')
    expect(asset.risks).toHaveLength(2)
    expect(detail.causalLinks).toHaveLength(3)
    const verified = detail.causalLinks.find((link) => link.status === 'verified')
    const pending = detail.causalLinks.find((link) => link.status === 'pending')
    const superseded = detail.causalLinks.find((link) => link.status === 'superseded')
    expect(verified?.chain[0]).toMatchObject({
      node: '美国 6 月 CPI 同比回落至 3.0%',
      kind: '事件',
      timeline_id: 1287,
    })
    expect(verified?.awaitVerification).toBe(false)
    expect(pending?.supersedesId).toBe(3)
    expect(superseded?.topic).toBe('美联储')
  })

  it('失败详情没有逐标的结论，未知 ID 返回 404', async () => {
    const failed = await mockApi.getResearchReport(2)
    expect(failed.error).not.toBe('')
    expect(failed.assetViews).toEqual([])
    expect(failed.causalLinks).toEqual([])
    const error: unknown = await mockApi.getResearchReport(99999).catch((item: unknown) => item)
    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).status).toBe(404)
  })
})

describe('mock 手动研报与实时状态', () => {
  it('手动生成后列表新增两个白名单结论', async () => {
    const before = (await mockApi.getResearchReports(0, 1)).total
    const result = await mockApi.runResearch('manual', 24)
    const after = await mockApi.getResearchReports(0, 1)
    expect(result).toMatchObject({ started: true, ok: true, assetCount: 2 })
    expect(after.total).toBe(before + 1)
    expect(after.items[0].roundId).toBe(result.roundId)
    expect(after.items[0].assetViews.map((view) => view.contract)).toEqual([
      'BTC_USDT',
      'ETH_USDT',
    ])
    expect(
      after.items[0].assetViews.every((view) =>
        ['确认', '冲突', '中性', '不可用'].includes(view.technicalConfirmation),
      ),
    ).toBe(true)
    const detail = await mockApi.getResearchReport(result.reportId!)
    expect(detail.globalRisks).toEqual(['临近美盘开盘，事件驱动风险上升'])
  })

  it('没有进行中研报轮', async () => {
    expect(await mockApi.getResearchLive()).toEqual({ round: null, tool_calls: [] })
  })
})
