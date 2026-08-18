/** 研报 mock：只模拟当前逐标的协议，失败报告不生成逐标的结论。
 *  实时研报轮：未点火时 getResearchLive 回 round null；runResearch 点火后先返进行中轮、
 *  再被轮询翻转为已结束（演示进度条「激活→轮询→退出→刷新」完整循环，与 mockReview 同模式）。
 *  runResearch 返回的 roundId 与随后 getResearchLive 进行中轮的 round_id 一致（模块级 liveRoundId 对齐）。 */
import { ApiError } from './http'
import type {
  ApiClient,
  CausalLinkView,
  ResearchAssetDetail,
  ResearchLive,
  ResearchReportDetail,
  ResearchReportSummary,
} from './types'

type ResearchMockHandlers = Pick<
  ApiClient,
  'getResearchReports' | 'getResearchReport' | 'runResearch' | 'getResearchLive'
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
    status: 'verified',
    brokenAt: null,
    topic: 'CPI',
    supersedesId: null,
    awaitVerification: false,
    time: nowIso(30),
  },
  {
    id: 3,
    reportId: 1,
    chain: [{ node: '美联储官员鹰派讲话', kind: '事件', timeline_id: 1290 }],
    confidence: 0.5,
    evidence: ['金十快讯'],
    status: 'superseded',
    brokenAt: null,
    topic: '美联储',
    supersedesId: null,
    awaitVerification: true,
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
    status: 'pending',
    brokenAt: null,
    topic: '美联储',
    supersedesId: 3,
    awaitVerification: true,
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
    verifyResult: '',
    time,
    ...overrides,
  }
}

const researchReports: ResearchReportDetail[] = [
  {
    id: 2,
    reportType: 'manual',
    schemaVersion: 2,
    summary: '',
    crossMarketView: '',
    globalRisks: [],
    assetViews: [],
    error: 'LLM 响应超时，本次研报失败',
    roundId: '',
    time: nowIso(6),
    causalLinks: [],
  },
  {
    id: 1,
    reportType: 'asia_open',
    schemaVersion: 2,
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
      }),
    ],
    error: '',
    roundId: 'rs-mock-1',
    time: nowIso(30),
    causalLinks,
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
    schemaVersion: 2,
    summary: '白名单合约整体处于震荡观察阶段，暂无高置信方向。',
    crossMarketView: 'BTC 与 ETH 同步缺少增量催化。',
    globalRisks: ['临近美盘开盘，事件驱动风险上升'],
    assetViews: [asset('BTC_USDT', hours, time), asset('ETH_USDT', hours, time)],
    error: '',
    roundId,
    time,
    causalLinks: [],
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
  }

  return { handlers }
}
