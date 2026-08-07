/**
 * mock 实现一致性测试：mockApi 的研报方法与 ApiClient 契约形态对齐
 * （分页/详情/手动触发/实时状态），供 VITE_USE_MOCK 预览与组件测试复用。
 * 注意 mock 为内存态：用例内一律取「操作前后」相对断言，不假定绝对条数；
 * 初始固定 2 条假研报（id=2 失败 roundId 空串、id=1 成功带因果链 roundId='rs-mock-1'）。
 */
import { describe, expect, it } from 'vitest'
import { ApiError } from '../api/http'
import { mockApi } from '../api/mock'

// 注意：runResearch 会向 mock 内存态追加研报（模块级状态），
// 「初始 2 条」断言必须先于文件内任何 runResearch 调用执行，故本用例置于文件顶部。
describe('mock 初始研报列表', () => {
  it('getResearchReports：初始固定 2 条、分页切片 + total 全量 + 列表 narrative 截断 200 字符', async () => {
    const page = await mockApi.getResearchReports(0, 2)
    expect(page.total).toBe(2)
    expect(page.items).toHaveLength(2)
    for (const item of page.items) expect(item.narrative.length).toBeLessThanOrEqual(200)
    // 最新在前：id=2 失败行在前、id=1 成功行在后
    expect(page.items[0].id).toBe(2)
    expect(page.items[0].error).not.toBe('')
    expect(page.items[1].id).toBe(1)
    // evidenceJson 与后端契约同形状：对象数组 [{point, source}] 的 JSON 原文
    const ev0 = JSON.parse(page.items[1].evidenceJson)[0]
    expect(ev0).toMatchObject({ point: expect.any(String), source: expect.any(String) })
    const rest = await mockApi.getResearchReports(2, 2)
    expect(rest.items).toHaveLength(0)
  })
})

describe('mock 实时研报状态', () => {
  it('getResearchLive：恒无进行中研报轮（round 为 null、tool_calls 为空），进度条保持隐藏', async () => {
    // 与 getReviewLive 的「无轮次」契约同形：研报轮要么瞬时完成、要么由 WS 事件驱动，不落实时轮
    const live = await mockApi.getResearchLive()
    expect(live.round).toBeNull()
    expect(live.tool_calls).toEqual([])
  })

  it('getResearchLive：手动触发研报后仍无进行中轮（研报完成即落库，不留实时状态）', async () => {
    await mockApi.runResearch()
    const live = await mockApi.getResearchLive()
    expect(live.round).toBeNull()
    expect(live.tool_calls).toEqual([])
  })
})

describe('mock 研报端点', () => {
  it('getResearchReport：详情 narrative 全文（长于列表截断）+ evidence 对象数组（{point,source}）+ risks 字符串数组 + 因果链 2 条；未知 id 抛 404 ApiError', async () => {
    const full = await mockApi.getResearchReport(1)
    expect(full.narrative.length).toBeGreaterThan(200) // 列表截断 200，详情给全文
    expect(full.evidence).toHaveLength(3)
    // mock 内部与后端契约同形状（{point,source} 对象数组），返回前经 http 同一适配逻辑转「point（source）」展示串
    expect(full.evidence[0]).toBe('美国 6 月 CPI 同比 3.0%，低于预期 3.1%（金十日历）')
    expect(full.risks).toHaveLength(2)
    expect(typeof full.risks[0]).toBe('string') // risks 为真字符串数组，不受对象适配影响
    expect(full.raw).toEqual({ direction: '偏多', confidence: '中', horizon: '24h' })
    // 因果链：已确认/待验证各一条，chain 均为 3 节点、confidence 为数值
    expect(full.causalLinks).toHaveLength(2)
    const verified = full.causalLinks.find((l) => l.status === 'verified')
    const pending = full.causalLinks.find((l) => l.status === 'pending')
    expect(verified).toBeDefined()
    expect(verified!.chain).toHaveLength(3)
    expect(verified!.chain[0]).toMatchObject({ node: '美国 6 月 CPI 同比回落至 3.0%', kind: '事件', timeline_id: 1287 })
    expect(typeof verified!.confidence).toBe('number')
    expect(pending).toBeDefined()
    expect(pending!.brokenAt).toBeNull()

    const err: unknown = await mockApi.getResearchReport(99999).catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).status).toBe(404)
  })

  it('getResearchReport：失败研报（id=2）详情为错误记录形态（error 非空、narrative 空、无因果链）', async () => {
    const failed = await mockApi.getResearchReport(2)
    expect(failed.error).not.toBe('')
    expect(failed.narrative).toBe('')
    expect(failed.causalLinks).toEqual([])
    expect(failed.roundId).toBe('') // 空串 = 无关联，演示工具链降级
  })

  it('runResearch：started/ok 且列表最前新增一条成功研报（reportId 与 roundId 自洽）', async () => {
    const before = (await mockApi.getResearchReports(0, 1)).total
    const result = await mockApi.runResearch('manual', 24)
    expect(result.started).toBe(true)
    expect(result.ok).toBe(true)
    const after = await mockApi.getResearchReports(0, 1)
    expect(after.total).toBe(before + 1)
    const newest = after.items[0]
    expect(newest.id).toBe(result.reportId)
    expect(newest.error).toBe('')
    // roundId 自洽：新增研报的 roundId 与 run 返回一致，且 getRound 对该 roundId 可回退通用详情
    expect(newest.roundId).toBe(result.roundId)
    expect(newest.roundId).not.toBe('')
  })

  it('roundId 自洽：非空 roundId 的研报经 getRound 可取到工具链；空串样例演示老研报降级', async () => {
    const list = await mockApi.getResearchReports(0, 50)
    const withRound = list.items.find((r) => r.roundId !== '')
    expect(withRound).toBeDefined()
    const detail = await mockApi.getRound(withRound!.roundId)
    expect(detail.round_id).toBe(withRound!.roundId)
    expect(Array.isArray(detail.tool_calls)).toBe(true)
    // 至少一条空串 roundId（演示「该研报无工具调用记录」降级形态）
    expect(list.items.some((r) => r.roundId === '')).toBe(true)
  })
})
