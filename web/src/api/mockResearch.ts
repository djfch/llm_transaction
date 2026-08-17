/** 研报 mock：只模拟当前逐标的协议，失败报告不生成逐标的结论。 */
import { ApiError } from './http'
import type {
  ApiClient,
  CausalLinkView,
  ResearchAssetDetail,
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

function runMockResearch(reportType: string, hours: number): number {
  const id = Math.max(0, ...researchReports.map((report) => report.id)) + 1
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
    roundId: 'rs-mock-' + id,
    time,
    causalLinks: [],
  })
  return id
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
      runMockResearch(reportType, hours)
      // 点火契约：立即返回 started + 回显参数；mock 同步落库一条新研报，演示列表刷新后出现新条目
      return reply({ started: true, reportType, hours })
    },
    getResearchLive: () => reply({ round: null, tool_calls: [] }),
  }

  return { handlers }
}
