/** 研报 mock：只模拟当前逐标的协议，失败报告不生成逐标的结论。
 *  实时研报轮：未点火时 getResearchLive 回 round null；runResearch 点火后先返进行中轮、
 *  再被轮询翻转为已结束（演示进度条「激活→轮询→退出→刷新」完整循环，与 mockReview 同模式）。
 *  runResearch 返回的 roundId 与随后 getResearchLive 进行中轮的 round_id 一致（模块级 liveRoundId 对齐）。
 *  研报提示词：模块级 researchPrompt（当前内容）+ researchPromptVersions（版本历史，最新在前）；
 *  保存/回滚都会前插新版本，diff 为 mock 级整体替换式 +/- 文本（同 mockReview 策略版本口径）。
 *  研报复盘：id=1 研报的 BTC 结论播种 researchReviews，演示详情页复盘块。 */
import { ApiError } from './http'
import type {
  ApiClient,
  CausalLinkView,
  ResearchAssetDetail,
  ResearchLive,
  ResearchPromptVersionDetail,
  ResearchReportDetail,
  ResearchReportSummary,
} from './types'

type ResearchMockHandlers = Pick<
  ApiClient,
  | 'getResearchReports'
  | 'getResearchReport'
  | 'runResearch'
  | 'getResearchLive'
  | 'getResearchPrompt'
  | 'putResearchPrompt'
  | 'getResearchPromptVersions'
  | 'getResearchPromptVersion'
  | 'getResearchPromptDiff'
  | 'rollbackResearchPrompt'
>

const nowIso = (hoursAgo: number) => new Date(Date.now() - hoursAgo * 3600_000).toISOString()

const causalLinks: CausalLinkView[] = [
  {
    id: 1,
    reportId: 1,
    chain: [
      { node: '美国 6 月 CPI 同比回落至 3.0%', kind: '事件', timeline_id: 1287 },
      { node: '美元指数走弱、实际利率下行', kind: '市场反应' },
      { node: '风险资产偏好修复，BTC 获增量资金流入', kind: '标的结论' },
    ],
    confidence: 0.72,
    evidence: ['金十日历：CPI 公布值低于预期'],
    status: 'concluded',
    topic: 'CPI',
    supersedesId: null,
    time: nowIso(30),
  },
  {
    id: 3,
    reportId: 1,
    chain: [{ node: '美联储官员鹰派讲话', kind: '事件', timeline_id: 1290 }],
    confidence: 0.5,
    evidence: ['金十快讯'],
    status: 'superseded',
    topic: '美联储',
    supersedesId: null,
    time: nowIso(30),
  },
  {
    id: 2,
    reportId: 1,
    chain: [
      { node: '美联储官员鹰派讲话', kind: '事件', timeline_id: 1290 },
      { node: '加密市场短线承压回落', kind: '标的结论' },
    ],
    confidence: 0.55,
    evidence: ['CME FedWatch'],
    status: 'tracking',
    topic: '美联储',
    supersedesId: 3,
    time: nowIso(29),
  },
]

function asset(
  contract: string,
  hours: number,
  time: string,
  overrides: Partial<ResearchAssetDetail> = {},
): ResearchAssetDetail {
  return {
    contract,
    direction: '中性',
    confidence: '低',
    horizon: hours + 'h',
    marketRegime: '震荡',
    technicalConfirmation: '中性',
    basisType: '结构延续',
    dataStatus: '完整',
    evidence: ['日线与 4 小时结构尚未形成突破'],
    risks: ['事件可能打破震荡结构'],
    narrative: contract + ' 暂无重要催化，等待价格、成交量与持仓量共同确认。',
    time,
    ...overrides,
  }
}

const researchReports: ResearchReportDetail[] = [
  {
    id: 2,
    reportType: 'manual',
    schemaVersion: 3,
    summary: '',
    crossMarketView: '',
    globalRisks: [],
    assetViews: [],
    error: 'LLM 响应超时，本次研报失败',
    roundId: '',
    time: nowIso(6),
    causalLinks: [],
    llmCredentialName: '',
    llmProvider: '',
    llmModel: '',
    llmThinkingEffort: '',
  },
  {
    id: 1,
    reportType: 'asia_open',
    schemaVersion: 3,
    summary: '亚盘风险偏好改善，BTC 技术结构获得确认。',
    crossMarketView: 'BTC 强于 ETH。',
    globalRisks: ['美联储官员讲话偏鹰', '亚盘流动性偏薄'],
    assetViews: [
      asset('BTC_USDT', 24, nowIso(30), {
        direction: '偏多',
        confidence: '中',
        marketRegime: '上涨趋势',
        technicalConfirmation: '确认',
        basisType: '混合',
        evidence: [
          '美国 6 月 CPI 同比 3.0%，低于预期 3.1%（金十日历）',
          'BTC 现货 ETF 连续三日净流入（律动快讯）',
          '资金费率维持中性，未见过热（Coinglass）',
        ],
        risks: ['美联储官员讲话偏鹰或压制风险偏好', '亚盘流动性偏薄，波动易被放大'],
        narrative:
          '亚盘时段宏观面偏多，CPI 低于预期，美元与实际利率回落；ETF 资金流和技术结构同步确认，但仍需防范鹰派讲话与薄流动性。',
        researchReviews: [
          {
            id: 2,
            reviewReportId: 1,
            directionRelation: 'realized',
            directionReason: '窗口内 BTC 上行约 5%，与偏多方向一致。',
            reasoningQuality: 'flawed',
            reasoningReview: '方向判断与 ETF 资金流证据一致，但宏观利多兑现的时点论证偏弱。',
            evidenceReviews: [
              {
                evidenceIndex: 0,
                factStatus: 'confirmed',
                reasoningStatus: 'supported',
                explanation: '金十日历核对：CPI 数值与公布时间引用准确。',
              },
              {
                evidenceIndex: 1,
                factStatus: 'confirmed',
                reasoningStatus: 'partially_supported',
                explanation: '律动快讯核对：ETF 净流入与方向判断一致，但缺具体规模数据。',
              },
              {
                evidenceIndex: 2,
                factStatus: 'contradicted',
                reasoningStatus: 'unsupported',
                explanation: '资金费率快照核对：方向对，但「中性」表述与原文「略偏正」有出入。',
              },
            ],
            confidenceAssessment: 'appropriate',
            confidenceReason: '与证据强度匹配。',
            improvementAdvice: '宏观事件类依据应给出兑现窗口的明确时间界。',
            outcome: { data_status: 'complete', candles_actual: 6, candles_expected: 6, price_start_at: '2026-08-22T08:00:00+00:00', price_end_at: '2026-08-22T09:30:00+00:00', start_price: 67400, end_price: 70800, return_pct: 5.04, high: 71500, max_up_pct: 6.1, low: 66600, max_down_pct: -1.2 },
            createdAt: nowIso(6),
          },
        ],
      }),
    ],
    error: '',
    roundId: 'rs-mock-1',
    time: nowIso(30),
    causalLinks,
    llmCredentialName: 'ds-main',
    llmProvider: 'openai_compat',
    llmModel: 'deepseek-v4-flash',
    llmThinkingEffort: '',
  },
]

function summaryOf(report: ResearchReportDetail): ResearchReportSummary {
  return {
    id: report.id,
    reportType: report.reportType,
    schemaVersion: report.schemaVersion,
    summary: report.summary,
    crossMarketView: report.crossMarketView,
    globalRisks: report.globalRisks,
    assetViews: report.assetViews.map((view) => ({
      contract: view.contract,
      direction: view.direction,
      confidence: view.confidence,
      horizon: view.horizon,
      marketRegime: view.marketRegime,
      technicalConfirmation: view.technicalConfirmation,
      basisType: view.basisType,
      dataStatus: view.dataStatus,
    })),
    error: report.error,
    roundId: report.roundId,
    time: report.time,
    llmCredentialName: report.llmCredentialName,
    llmProvider: report.llmProvider,
    llmModel: report.llmModel,
    llmThinkingEffort: report.llmThinkingEffort,
  }
}

/** 手动研报：同步落库一条新研报（演示列表刷新后出现新条目）。返回新研报 ID 与其审计轮 ID（点火响应与 /live 进行中轮共用）。 */
function runMockResearch(reportType: string, hours: number): { id: number; roundId: string } {
  const id = Math.max(0, ...researchReports.map((report) => report.id)) + 1
  const roundId = 'rs-mock-' + id
  const time = new Date().toISOString()
  researchReports.unshift({
    id,
    reportType,
    schemaVersion: 3,
    summary: '白名单合约整体处于震荡观察阶段，暂无高置信方向。',
    crossMarketView: 'BTC 与 ETH 同步缺少增量催化。',
    globalRisks: ['临近美盘开盘，事件驱动风险上升'],
    assetViews: [asset('BTC_USDT', hours, time), asset('ETH_USDT', hours, time)],
    error: '',
    roundId,
    time,
    causalLinks: [],
    llmCredentialName: 'ds-main',
    llmProvider: 'openai_compat',
    llmModel: 'deepseek-v4-flash',
    llmThinkingEffort: '',
  })
  return { id, roundId }
}

/** 点火后进行中轮被轮询的次数预算：首次轮询返进行中，第 2 次起翻转已结束（演示进度条完整进出循环） */
const ACTIVE_POLLS_AFTER_IGNITE = 2

/** 研报轮进行状态：null = 从未点火（getResearchLive 回 round null）；true/false = 进行中/已结束 */
let liveRoundActive: boolean | null = null
let activePollsLeft = 0
/** 进行中研报轮 ID：runResearch 点火时换成本轮的预分配 ID（与点火响应 roundId 一致） */
let liveRoundId = 'rs-live-mock'

/**
 * 实时研报审计轮样例（active=true 进行中 / false 已结束，工具链两种形态下都保留）。
 * 进行中：ended_at 为 null、llm_raw 空串（与 /api/research/live 同约定）；已结束：ended_at 非空、带结论 llm_raw。
 * 每次请求重建以保证 started_at 始终新鲜（不触发前端 30 分钟僵尸轮防线）。
 */
function buildResearchLive(active: boolean): ResearchLive {
  const startedAt = Math.floor(Date.now() / 1000) - 15
  return {
    round: {
      round_id: liveRoundId,
      wake_source: 'research',
      prompt_md5: '3f4a5b6c7d8e9f00112233445566778899aabb',
      prompt_snapshot: '# 研报 Agent Prompt（md5: 3f4a…aabb）\n\n你是宏观与消息面前瞻研报 Agent。',
      context_snapshot: '研报类型: manual\n窗口: 最近 24 小时\n白名单: BTC_USDT, ETH_USDT',
      llm_raw: active ? '' : JSON.stringify({ thoughts: '白名单合约整体处于震荡观察阶段，暂无高置信方向。' }),
      started_at: startedAt,
      ended_at: active ? null : startedAt + 30,
      error: '',
    },
    tool_calls: [
      {
        seq: 1,
        tool: 'get_research_market_data',
        args: { contract: 'BTC_USDT' },
        risk_verdict: '',
        risk_reason: '',
        result: { text: 'BTC_USDT 4h/1d 快照：震荡结构，量能平稳' },
        duration_ms: 11,
      },
      {
        seq: 2,
        tool: 'get_news_flash',
        args: { keyword: '美联储' },
        risk_verdict: '',
        risk_reason: '',
        result: { text: '近 24 小时无重大突发利空' },
        duration_ms: 8,
      },
    ],
  }
}

// ---- 研报提示词 mock：当前内容 + 版本历史（最新在前），保存/回滚均前插新版本 ----

/** 研报提示词当前内容（与首个 applied 版本 v3 同文，保持叙事自洽） */
let researchPrompt = '# 研报 Agent Prompt\n\n你是宏观与消息面前瞻研报 Agent。\n\n## 决策规则\n- 只输出白名单合约的方向结论\n'

const researchPromptVersions: ResearchPromptVersionDetail[] = [
  {
    id: 4,
    md5: 'd4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9',
    createdBy: 'review_agent',
    reason: '复盘建议：宏观事件依据需给出兑现窗口（草稿待生效）',
    reviewReportId: 2,
    time: nowIso(2),
    status: 'draft',
    content: researchPrompt + '- 宏观事件依据须注明兑现时间窗口\n',
  },
  {
    id: 3,
    md5: '3f4a5b6c7d8e9f00112233445566778899aabb',
    createdBy: 'review_agent',
    reason: '复盘修订：收紧高置信门槛',
    reviewReportId: 1,
    time: nowIso(20),
    status: 'applied',
    content: researchPrompt,
  },
  {
    id: 2,
    md5: '2f3a4b5c6d7e8f90112233445566778899aabb',
    createdBy: 'review_agent',
    reason: '复盘修订草稿（未生效即被 v3 取代）',
    reviewReportId: 1,
    time: nowIso(26),
    status: 'discarded',
    content: '# 研报 Agent Prompt\n\n你是宏观与消息面前瞻研报 Agent。\n',
  },
  {
    id: 1,
    md5: '1f2a3b4c5d6e7f890112233445566778899aab',
    createdBy: 'human',
    reason: '初始版本',
    reviewReportId: null,
    time: nowIso(50),
    status: 'applied',
    content: '# 研报 Agent Prompt\n\n你是宏观与消息面前瞻研报 Agent。\n',
  },
]

/** 前插一个 applied 版本并写回当前内容；返回新版本号 */
function applyPromptVersion(content: string, createdBy: 'human' | 'rollback', reason: string): number {
  researchPrompt = content
  const id = Math.max(0, ...researchPromptVersions.map((v) => v.id)) + 1
  researchPromptVersions.unshift({
    id,
    md5: 'mock-md5-' + id,
    createdBy,
    reason,
    reviewReportId: null,
    time: new Date().toISOString(),
    status: 'applied',
    content,
  })
  return id
}

/** mock 级整体替换式文本 diff（同 mockReview 策略版本口径）：同文返 ''，否则逐行 - 旧 / + 新 */
function mockPromptDiff(fromId: number, toId: number): string {
  const from = researchPromptVersions.find((v) => v.id === fromId)
  const to = researchPromptVersions.find((v) => v.id === toId)
  if (!from || !to) throw new ApiError(404, '研报提示词版本不存在: ' + (!from ? fromId : toId))
  if (from.content === to.content) return ''
  const removed = from.content.split('\n').map((line) => '- ' + line)
  const added = to.content.split('\n').map((line) => '+ ' + line)
  return [`--- v${fromId}`, `+++ v${toId}`, ...removed, ...added].join('\n')
}

export function createResearchMock(reply: <T>(value: T) => Promise<T>) {
  const handlers: ResearchMockHandlers = {
    getResearchReports: (offset, limit) =>
      reply({
        items: researchReports.slice(offset, offset + limit).map(summaryOf),
        total: researchReports.length,
      }),
    getResearchReport: (id) => {
      const report = researchReports.find((item) => item.id === id)
      return report
        ? reply(structuredClone(report))
        : Promise.reject(new ApiError(404, '研报不存在: ' + id))
    },
    runResearch: (reportType = 'manual', hours = 24) => {
      const { roundId } = runMockResearch(reportType, hours)
      // 点火契约：立即返回 started + 回显参数 + 预分配审计轮 ID（与随后 /live 进行中轮 round_id 一致，同后端契约）
      // 每次点火重新进入进行中轮：前轮询返进行中，随后翻转已结束（演示进度条「激活→退出→onFinished」循环）
      liveRoundId = roundId
      liveRoundActive = true
      activePollsLeft = ACTIVE_POLLS_AFTER_IGNITE
      return reply({ started: true, reportType, hours, roundId })
    },
    getResearchLive: (roundId) => {
      // 按 ID 直查：mock 只有一个实时轮，ID 不符即查无此轮（与后端契约一致：round null + 空工具链）
      if (roundId !== undefined && roundId !== liveRoundId) return reply({ round: null, tool_calls: [] })
      if (liveRoundActive === null) return reply({ round: null, tool_calls: [] })
      if (liveRoundActive) {
        activePollsLeft -= 1
        if (activePollsLeft <= 0) liveRoundActive = false
      }
      return reply(buildResearchLive(liveRoundActive))
    },
    getResearchPrompt: () => reply(researchPrompt),
    putResearchPrompt: (content) => {
      applyPromptVersion(content, 'human', '前端手动保存')
      return reply(content)
    },
    // 列表项剥掉 content 全文（与后端契约一致：列表不含正文）
    getResearchPromptVersions: () =>
      reply(
        researchPromptVersions.map((v) => ({
          id: v.id,
          md5: v.md5,
          createdBy: v.createdBy,
          reason: v.reason,
          reviewReportId: v.reviewReportId,
          time: v.time,
          status: v.status,
        })),
      ),
    getResearchPromptVersion: (id) => {
      const version = researchPromptVersions.find((v) => v.id === id)
      return version
        ? reply(structuredClone(version))
        : Promise.reject(new ApiError(404, '研报提示词版本不存在: ' + id))
    },
    getResearchPromptDiff: (fromId, toId) => reply(mockPromptDiff(fromId, toId)),
    rollbackResearchPrompt: (id) => {
      const target = researchPromptVersions.find((v) => v.id === id)
      if (!target) return Promise.reject(new ApiError(404, '研报提示词版本不存在: ' + id))
      const newId = applyPromptVersion(target.content, 'rollback', '回滚到 v' + id)
      return reply({ rolledBackTo: id, version: newId })
    },
  }

  return { handlers }
}
