/**
 * 复盘/策略版本 mock：独立内存态（复盘报告、策略版本表）+ 7 个 ApiClient 方法实现，
 * 由 mock.ts 经 createReviewMock 装配进 mockApi，本模块独立维护复盘模拟状态。
 * 与后端契约对齐：报告列表 reportMd 截断 200 字符、版本列表不含 content、
 * 手动复盘立即出一份「未调整」报告、回滚 = 写回历史内容 + 记 rollback 新版本。
 */
import { ApiError } from './http'
import type { ApiClient, ReviewReport, StrategyVersionDetail } from './types'

/** mockApi 中复盘/策略版本相关的方法子集 */
type ReviewMockHandlers = Pick<
  ApiClient,
  | 'getReviewReports'
  | 'getReviewReport'
  | 'runReview'
  | 'getStrategyVersions'
  | 'getStrategyVersion'
  | 'getStrategyDiff'
  | 'rollbackStrategy'
>

/** 策略原文读写引用（strategy 变量由 mock.ts 持有，本模块经回调同步写回） */
interface StrategyRef {
  get: () => string
  set: (content: string) => void
}

/** 复盘报告假数据（最新在前）：r3 失败红字行 / r2·r1 各改写出一个版本；reportMd 超 200 字符演示列表截断。 */
const reviewReports: ReviewReport[] = [
  {
    id: 3,
    periodStart: new Date(Date.now() - 36 * 3600_000).toISOString(),
    periodEnd: new Date(Date.now() - 12 * 3600_000).toISOString(),
    statsJson: '{}',
    reportMd: '',
    strategyAction: 'none',
    newVersionId: null,
    error: 'LLM 响应超时，本次复盘失败',
    roundId: '', // 空串 = 无关联，演示「老报告」降级
    time: new Date(Date.now() - 11 * 3600_000).toISOString(),
  },
  {
    id: 2,
    periodStart: new Date(Date.now() - 60 * 3600_000).toISOString(),
    periodEnd: new Date(Date.now() - 36 * 3600_000).toISOString(),
    statsJson:
      '{"close_count":5,"total_pnl":"-32.10","win_count":2,"win_rate":"0.4000","total_profit":"45.20","total_loss":"-77.30","profit_factor":"0.5847","avg_win":"22.60000000","avg_loss":"-25.76666667","max_loss":"-40.10","per_contract":{"BTC_USDT":{"count":3,"pnl":"-20.10"},"ETH_USDT":{"count":2,"pnl":"-12.00"}}}',
    reportMd:
      '# 复盘报告\n\n本区间平仓 5 笔，胜率 40.00%，盈亏比 0.58，总盈亏 -32.10 USDT。\n\n## 归因\n\n- BTC 逆势加仓两笔均止损离场，贡献主要亏损。\n- ETH 对冲仓位盈利被过早止盈，拉低盈亏比。\n- 资金费率偏高时段仍持有多头，持仓成本侵蚀利润。\n\n## 调整\n\n已改写策略书（v3）：收紧止损并限制逆势加仓。\n\n以上结论由复盘 Agent 自动生成，仅供参考；策略调整已自动生效并版本化留痕。',
    strategyAction: 'rewrite',
    newVersionId: 3,
    error: '',
    roundId: '9f3ab2c1d4e54f01', // 演示工具链内嵌：mock getRound 对未知 roundId 回退通用详情
    time: new Date(Date.now() - 35 * 3600_000).toISOString(),
  },
  {
    id: 1,
    periodStart: new Date(Date.now() - 84 * 3600_000).toISOString(),
    periodEnd: new Date(Date.now() - 60 * 3600_000).toISOString(),
    statsJson:
      '{"close_count":4,"total_pnl":"61.80","win_count":3,"win_rate":"0.7500","total_profit":"88.20","total_loss":"-26.40","profit_factor":"3.3409","avg_win":"29.40000000","avg_loss":"-13.20000000","max_loss":"-18.50","per_contract":{"BTC_USDT":{"count":4,"pnl":"61.80"}}}',
    reportMd:
      '# 复盘报告\n\n本区间平仓 4 笔，胜率 75.00%，盈亏比 3.34，总盈亏 +61.80 USDT。\n\n## 归因\n\n- 趋势跟随信号质量高，三笔盈利均来自 BTC 突破行情。\n- 唯一亏损为止盈过早后的回撤单。\n- 高波动时段仓位偏保守，错失部分趋势延续收益。\n\n## 调整\n\n已改写策略书（v2）：延长盈利持仓时间，避免过早止盈。\n\n以上结论由复盘 Agent 自动生成，仅供参考；策略调整已自动生效并版本化留痕。',
    strategyAction: 'rewrite',
    newVersionId: 2,
    error: '',
    roundId: '', // 空串 = 无关联，演示「老报告」降级
    time: new Date(Date.now() - 59 * 3600_000).toISOString(),
  },
]

let strategyVersions: StrategyVersionDetail[] = []

/** 下一个策略版本号（版本表自增） */
function nextVersionId(): number {
  return Math.max(0, ...strategyVersions.map((v) => v.id)) + 1
}

/** 头部插入新版本（版本表恒保持最新在前） */
function prependVersion(version: StrategyVersionDetail): void {
  strategyVersions = [version, ...strategyVersions]
}

/** 播种三个版本（最新在前）：v1 人工初始 → v2/v3 复盘改写；v3 与当前策略原文同文（「当前版本」叙事自洽）。 */
function seedVersions(currentStrategy: string): void {
  strategyVersions = [
    {
      id: 3,
      content: currentStrategy,
      md5: 'c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8',
      createdBy: 'review_agent',
      reason: '复盘：胜率下滑，收紧止损并限制逆势加仓',
      reportId: 2,
      time: new Date(Date.now() - 12 * 3600_000).toISOString(),
    },
    {
      id: 2,
      content: `# 系统提示词（system_prompt.md）\n\n你是 Gate.io USDT 永续合约的自主交易 Agent。\n\n## 原则\n\n1. 保本优先，单笔风险不超过权益的 2%。\n2. 只交易白名单合约，遵守风控参数。\n3. 延长盈利持仓时间，避免过早止盈。\n`,
      md5: 'b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7',
      createdBy: 'review_agent',
      reason: '复盘：盈亏比偏低，延长盈利持仓时间',
      reportId: 1,
      time: new Date(Date.now() - 36 * 3600_000).toISOString(),
    },
    {
      id: 1,
      content: `# 系统提示词（system_prompt.md）\n\n你是 Gate.io USDT 永续合约的自主交易 Agent。\n\n## 原则\n\n1. 保本优先。\n2. 只交易白名单合约。\n`,
      md5: 'a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6',
      createdBy: 'human',
      reason: '初始版本',
      reportId: null,
      time: new Date(Date.now() - 72 * 3600_000).toISOString(),
    },
  ]
}

/** 手动复盘：立即出一份「未调整」报告（最新在前），演示成功后列表刷新。 */
function runMockReview(): number {
  const newId = Math.max(0, ...reviewReports.map((r) => r.id)) + 1
  const end = Date.now()
  reviewReports.unshift({
    id: newId,
    periodStart: new Date(end - 24 * 3600_000).toISOString(),
    periodEnd: new Date(end).toISOString(),
    statsJson:
      '{"close_count":3,"total_pnl":"18.40","win_count":2,"win_rate":"0.6667","total_profit":"30.10","total_loss":"-11.70","profit_factor":"2.5726","avg_win":"15.05000000","avg_loss":"-11.70000000","max_loss":"-11.70","per_contract":{"BTC_USDT":{"count":3,"pnl":"18.40"}}}',
    reportMd:
      '# 复盘报告\n\n本区间平仓 3 笔，胜率 66.67%，盈亏比 2.57，总盈亏 +18.40 USDT。\n\n策略执行符合预期，无需调整。\n\n以上结论由复盘 Agent 自动生成，仅供参考。',
    strategyAction: 'none',
    newVersionId: null,
    error: '',
    roundId: `rv-mock-${newId}`, // 与下方 runReview 返回的 roundId 自洽，演示新报告内嵌工具链
    time: new Date(end).toISOString(),
  })
  return newId
}

/**
 * 创建复盘/策略版本 mock 域：播种版本表并返回 7 个 ApiClient 方法 + 两个协作钩子。
 * reply / strategy 读写由 mock.ts 注入，本模块不反向依赖 mock.ts（避免循环导入）。
 */
export function createReviewMock(reply: <T>(value: T) => Promise<T>, strategyRef: StrategyRef) {
  seedVersions(strategyRef.get())

  const handlers: ReviewMockHandlers = {
    getReviewReports: (offset, limit) =>
      // 与后端契约一致：列表项 reportMd 截断 200 字符，详情端点给全文
      reply({
        items: reviewReports.slice(offset, offset + limit).map((r) => ({ ...r, reportMd: r.reportMd.slice(0, 200) })),
        total: reviewReports.length,
      }),
    getReviewReport: (id) => {
      const report = reviewReports.find((r) => r.id === id)
      if (!report) return Promise.reject(new ApiError(404, `复盘报告不存在: ${id}`))
      return reply({ ...report })
    },
    runReview: () => {
      const newId = runMockReview()
      return reply({
        started: true,
        ok: true,
        reportId: newId,
        roundId: `rv-mock-${newId}`,
        strategyAction: 'none',
        newVersionId: null,
        error: '',
      })
    },
    getStrategyVersions: () =>
      // 与后端契约一致：列表不含 content 全文（省流量）
      reply(
        strategyVersions.map((v) => ({
          id: v.id,
          md5: v.md5,
          createdBy: v.createdBy,
          reason: v.reason,
          reportId: v.reportId,
          time: v.time,
        })),
      ),
    getStrategyVersion: (id) => {
      const version = strategyVersions.find((v) => v.id === id)
      if (!version) return Promise.reject(new ApiError(404, `策略版本不存在: ${id}`))
      return reply({ ...version })
    },
    getStrategyDiff: (fromId, toId) => {
      const from = strategyVersions.find((v) => v.id === fromId)
      const to = strategyVersions.find((v) => v.id === toId)
      if (!from || !to) return Promise.reject(new ApiError(404, `策略版本不存在: ${from ? toId : fromId}`))
      // mock 级 diff：非最小 LCS，仅整体替换式 +/- 行，供预览着色演示
      if (from.content === to.content) return reply('') // 与后端 difflib 一致：同文 diff 为空串
      const header = [`--- v${fromId}`, `+++ v${toId}`]
      const lines = [...header, '@@ 全文对比 @@']
      for (const l of from.content.split('\n')) lines.push(`-${l}`)
      for (const l of to.content.split('\n')) lines.push(`+${l}`)
      return reply(lines.join('\n'))
    },
    rollbackStrategy: (id) => {
      const target = strategyVersions.find((v) => v.id === id)
      if (!target) return Promise.reject(new ApiError(404, `策略版本不存在: ${id}`))
      // 与后端对齐：回滚 = 写回历史内容 + 记 rollback 新版本；同步更新 mock 策略原文
      strategyRef.set(target.content)
      const newId = nextVersionId()
      prependVersion({
        id: newId,
        content: target.content,
        md5: target.md5,
        createdBy: 'rollback',
        reason: `回滚到 v${id}`,
        reportId: null,
        time: new Date().toISOString(),
      })
      return reply({ rolledBackTo: id, version: newId })
    },
  }

  return {
    handlers,
    /** 人工保存策略即落 human 新版本（与后端 StrategyStore 对齐；md5 为占位值，仅供预览关联演示） */
    addHumanVersion(content: string): void {
      prependVersion({
        id: nextVersionId(),
        content,
        md5: `mock-md5-v${nextVersionId()}`,
        createdBy: 'human',
        reason: '人工在线编辑',
        reportId: null,
        time: new Date().toISOString(),
      })
    },
    /** 版本表 md5 列表（最新在前，供 mock.ts 决策轮 strategyMd5 关联演示） */
    versionMd5s(): string[] {
      return strategyVersions.map((v) => v.md5)
    },
  }
}
