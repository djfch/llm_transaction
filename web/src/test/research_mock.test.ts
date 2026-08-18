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
  // 注意：getResearchLive 的模块级状态会被 runResearch 点火（null → 进行中 → 已结束），
  // 「未点火无研报轮」断言必须先于文件内任何 runResearch 调用执行，故本用例置于 describe 顶部。
  it('未点火前无进行中研报轮', async () => {
    expect(await mockApi.getResearchLive()).toEqual({ round: null, tool_calls: [] })
  })

  it('手动生成后列表新增两个白名单结论', async () => {
    const before = (await mockApi.getResearchReports(0, 1)).total
    const result = await mockApi.runResearch('manual', 24)
    const after = await mockApi.getResearchReports(0, 1)
    // 点火契约：返回 started + 回显参数；新研报已同步落库，从列表取最新条目核对
    expect(result).toMatchObject({ started: true, reportType: 'manual', hours: 24 })
    expect(after.total).toBe(before + 1)
    expect(after.items[0].assetViews.map((view) => view.contract)).toEqual([
      'BTC_USDT',
      'ETH_USDT',
    ])
    expect(
      after.items[0].assetViews.every((view) =>
        ['确认', '冲突', '中性', '不可用'].includes(view.technicalConfirmation),
      ),
    ).toBe(true)
    const detail = await mockApi.getResearchReport(after.items[0].id)
    expect(detail.globalRisks).toEqual(['临近美盘开盘，事件驱动风险上升'])
  })

  it('点火后先返进行中轮、再轮询翻转为已结束；再次点火重新进入进行中轮', async () => {
    const result = await mockApi.runResearch('manual', 24)
    const activeLive = await mockApi.getResearchLive()
    expect(activeLive.round).not.toBeNull()
    expect(activeLive.round!.wake_source).toBe('research')
    expect(activeLive.round!.ended_at).toBeNull() // 进行中
    // 点火响应的 roundId 与 /live 进行中轮 round_id 一致（同后端契约）
    expect(activeLive.round!.round_id).toBe(result.roundId)
    expect(activeLive.tool_calls.length).toBeGreaterThanOrEqual(2)
    // 工具 result 一律 {text} 包装（与后端 research agent 对齐）
    for (const call of activeLive.tool_calls) expect(call.result).toHaveProperty('text')

    const doneLive = await mockApi.getResearchLive()
    expect(doneLive.round!.ended_at).not.toBeNull() // 已结束
    expect(doneLive.round!.llm_raw).not.toBe('')

    // 第二次点火也有完整进出循环
    await mockApi.runResearch('manual', 24)
    const reignited = await mockApi.getResearchLive()
    expect(reignited.round!.ended_at).toBeNull()
  })
})
