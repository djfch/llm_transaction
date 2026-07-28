/**
 * mock 实现一致性测试：mockApi 的复盘/策略版本方法与 ApiClient 契约形态对齐
 * （分页/详情/手动复盘/版本列表/详情/diff/回滚），供 VITE_USE_MOCK 预览与组件测试复用。
 * 注意 mock 为内存态：用例内一律取「操作前后」相对断言，不假定绝对条数。
 */
import { describe, expect, it } from 'vitest'
import { ApiError } from '../api/http'
import { mockApi } from '../api/mock'

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

  it('runReview：started/ok 且列表最前新增一条「未调整」报告', async () => {
    const before = (await mockApi.getReviewReports(0, 1)).total
    const result = await mockApi.runReview()
    expect(result.started).toBe(true)
    expect(result.ok).toBe(true)
    const after = await mockApi.getReviewReports(0, 1)
    expect(after.total).toBe(before + 1)
    expect(after.items[0].strategyAction).toBe('none')
    expect(after.items[0].id).toBe(result.reportId)
    expect(after.items[0].error).toBe('')
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
