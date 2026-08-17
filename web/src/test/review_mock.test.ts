/**
 * mock 实现一致性测试：mockApi 的复盘/策略版本方法与 ApiClient 契约形态对齐
 * （分页/详情/手动复盘/版本列表/详情/diff/回滚），供 VITE_USE_MOCK 预览与组件测试复用。
 * 注意 mock 为内存态：用例内一律取「操作前后」相对断言，不假定绝对条数。
 */
import { describe, expect, it } from 'vitest'
import { ApiError } from '../api/http'
import { mockApi } from '../api/mock'

// 注意：getReviewLive 的模块级状态会被 runReview 翻转（进行中 → 已结束），
// 「默认进行中」断言必须先于文件内任何 runReview 调用执行，故本 describe 置于文件顶部。
describe('mock 实时复盘状态', () => {
  it('getReviewLive：默认返回进行中复盘轮（ended_at 为 null、wake_source=review、工具链非空且叙事自洽）', async () => {
    const live = await mockApi.getReviewLive()
    expect(live.round).not.toBeNull()
    const round = live.round!
    expect(round.wake_source).toBe('review')
    expect(round.ended_at).toBeNull()
    expect(round.error).toBe('')
    expect(live.tool_calls.length).toBeGreaterThanOrEqual(2)
    expect(live.tool_calls[0].tool).toBe('get_review_stats')
    // 工具 result 一律 {text} 包装（与后端 review agent 对齐）
    for (const call of live.tool_calls) expect(call.result).toHaveProperty('text')
    // started_at 新鲜：不触发前端 30 分钟僵尸轮防线
    expect(Date.now() - round.started_at * 1000).toBeLessThan(30 * 60 * 1000)
    // strategy_md5 关联版本表最新版（与当前策略同文，叙事自洽）
    const versions = await mockApi.getStrategyVersions()
    expect(round.strategy_md5).toBe(versions[0].md5)
  })

  it('getReviewLive：手动复盘后先返进行中轮、再轮询翻转为已结束（演示进度条完整进出循环）', async () => {
    await mockApi.runReview()
    const activeLive = await mockApi.getReviewLive()
    expect(activeLive.round!.ended_at).toBeNull() // 进行中
    const doneLive = await mockApi.getReviewLive()
    const round = doneLive.round!
    expect(round.ended_at).not.toBeNull()
    expect(round.ended_at!).toBeGreaterThan(round.started_at)
    expect(round.llm_raw).not.toBe('')
    expect(doneLive.tool_calls.length).toBeGreaterThanOrEqual(2)
  })

  it('getReviewLive：再次手动复盘重新进入进行中轮（每次点火都有完整进出循环）', async () => {
    await mockApi.runReview()
    const live = await mockApi.getReviewLive()
    expect(live.round).not.toBeNull()
    expect(live.round!.ended_at).toBeNull()
  })
})

describe('mock 复盘端点', () => {
  it('getReviewReports：分页切片 + total 全量 + 列表 reportMd 截断 200 字符', async () => {
    const page = await mockApi.getReviewReports(0, 2)
    expect(page.total).toBeGreaterThanOrEqual(3)
    expect(page.items).toHaveLength(2)
    for (const item of page.items) expect(item.reportMd.length).toBeLessThanOrEqual(200)
    const rest = await mockApi.getReviewReports(2, 2)
    expect(rest.items.length).toBeGreaterThanOrEqual(1)
    expect(rest.items[0].id).not.toBe(page.items[0].id)
  })

  it('getReviewReport：详情 reportMd 全文（长于列表截断）；未知 id 抛 404 ApiError', async () => {
    const list = await mockApi.getReviewReports(0, 50)
    const rewrite = list.items.find((r) => r.strategyAction === 'rewrite')
    expect(rewrite).toBeDefined()
    const full = await mockApi.getReviewReport(rewrite!.id)
    expect(full.reportMd.length).toBeGreaterThan(200)
    expect(full.strategyAction).toBe('rewrite')
    expect(full.newVersionId).not.toBeNull()
    const err: unknown = await mockApi.getReviewReport(99999).catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).status).toBe(404)
  })

  it('runReview：点火返回 started + 区间回显，且列表最前新增一条「未调整」报告', async () => {
    const before = (await mockApi.getReviewReports(0, 1)).total
    const result = await mockApi.runReview()
    // 点火契约：仅 started + 统计区间回显，不含执行结果
    expect(result.started).toBe(true)
    expect(result.periodEnd!).toBeGreaterThan(result.periodStart!)
    const after = await mockApi.getReviewReports(0, 1)
    expect(after.total).toBe(before + 1)
    expect(after.items[0].strategyAction).toBe('none')
    expect(after.items[0].error).toBe('')
  })

  it('roundId 自洽：非空 roundId 的报告经 getRound 可取到工具链；空串样例演示老报告降级', async () => {
    const list = await mockApi.getReviewReports(0, 50)
    // 至少一条带非空 roundId（演示工具链内嵌），且 getRound 对任意 roundId 回退通用详情
    const withRound = list.items.find((r) => r.roundId !== '')
    expect(withRound).toBeDefined()
    const detail = await mockApi.getRound(withRound!.roundId)
    expect(detail.round_id).toBe(withRound!.roundId)
    expect(Array.isArray(detail.tool_calls)).toBe(true)
    // 至少一条空串 roundId（功能上线前的老报告降级形态）
    expect(list.items.some((r) => r.roundId === '')).toBe(true)
  })
})

describe('mock 策略版本端点', () => {
  it('getStrategyVersions：至少 3 个版本、最新在前、列表不含 content', async () => {
    const versions = await mockApi.getStrategyVersions()
    expect(versions.length).toBeGreaterThanOrEqual(3)
    expect('content' in versions[0]).toBe(false)
    const ids = versions.map((v) => v.id)
    expect(ids).toEqual([...ids].sort((a, b) => b - a))
  })

  it('getStrategyVersion：详情含 content 全文；未知 id 抛 404 ApiError', async () => {
    const [latest] = await mockApi.getStrategyVersions()
    const detail = await mockApi.getStrategyVersion(latest.id)
    expect(detail.id).toBe(latest.id)
    expect(detail.content.length).toBeGreaterThan(0)
    const err: unknown = await mockApi.getStrategyVersion(99999).catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).status).toBe(404)
  })

  it('getStrategyDiff：返回带 ---/+++ 头部与 +/- 行的纯文本', async () => {
    const text = await mockApi.getStrategyDiff(1, 2)
    expect(text).toContain('--- v1')
    expect(text).toContain('+++ v2')
    const lines = text.split('\n')
    expect(lines.some((l) => l.startsWith('-') && !l.startsWith('---'))).toBe(true)
    expect(lines.some((l) => l.startsWith('+') && !l.startsWith('+++'))).toBe(true)
    const err: unknown = await mockApi.getStrategyDiff(1, 99999).catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
  })

  it('getStrategyDiff：同文版本返回空串（与后端 difflib unified_diff 行为一致）', async () => {
    expect(await mockApi.getStrategyDiff(1, 1)).toBe('')
  })

  it('rollbackStrategy：生成 rollback 新版本（同 md5）并写回策略原文', async () => {
    const before = await mockApi.getStrategyVersions()
    const target = before[before.length - 1] // 最旧版本
    const result = await mockApi.rollbackStrategy(target.id)
    expect(result.rolledBackTo).toBe(target.id)

    const after = await mockApi.getStrategyVersions()
    expect(after).toHaveLength(before.length + 1)
    expect(after[0].id).toBe(result.version)
    expect(after[0].createdBy).toBe('rollback')
    expect(after[0].md5).toBe(target.md5) // 回滚同文：md5 与目标版本一致
    // 策略原文已写回目标版本内容（编辑器重拉即为此文本）
    const targetDetail = await mockApi.getStrategyVersion(target.id)
    expect(await mockApi.getStrategy()).toBe(targetDetail.content)

    const err: unknown = await mockApi.rollbackStrategy(99999).catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).status).toBe(404)
  })
})

describe('mock 决策轮策略关联', () => {
  it('getRounds/getRound 携带 strategyMd5（含空串降级样例）', async () => {
    const page = await mockApi.getRounds(0, 5)
    expect(page.items.every((r) => typeof r.strategyMd5 === 'string')).toBe(true)
    const withMd5 = page.items.find((r) => r.strategyMd5 !== '')
    expect(withMd5).toBeDefined()
    const detail = await mockApi.getRound(withMd5!.round_id)
    expect(detail.strategyMd5).toBe(withMd5!.strategyMd5)
  })
})

