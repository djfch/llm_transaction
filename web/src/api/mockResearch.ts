/**
 * 研报 mock：独立内存态（研报列表，含详情字段与因果链演示数据）+ 4 个 ApiClient 方法实现，
 * 由 mock.ts 经 createResearchMock 装配进 mockApi，本模块独立维护研报模拟状态。
 * 与后端契约对齐：列表 narrative 截断 200 字符、详情给全文 + evidence/risks/raw 已解析 + causalLinks；
 * evidence 内部数据与后端同形状（{point, source} 对象数组），返回前经 http 同一适配逻辑转展示串；
 * 手动研报立即出一份成功研报（演示列表刷新）；getResearchLive 恒无进行中轮（进度条保持隐藏）。
 */
import { ApiError, adaptEvidenceList } from './http'
import type { ApiClient, ResearchReportDetail } from './types'

/** mockApi 中研报相关的方法子集 */
type ResearchMockHandlers = Pick<
  ApiClient,
  'getResearchReports' | 'getResearchReport' | 'runResearch' | 'getResearchLive'
>

/** 证据原始项：与后端契约同形状（{point, source} 对象数组；source 可缺省） */
interface MockEvidenceItem {
  point: string
  source?: string
}

/** 研报详情 mock 内部形状：evidence 存后端原始对象数组，返回前经 adaptEvidenceList 适配为展示串 */
type MockResearchDetail = Omit<ResearchReportDetail, 'evidence'> & { evidence: MockEvidenceItem[] }

/**
 * 研报假数据（最新在前）：r2 失败红字行（roundId 空串，演示工具链降级文案）；
 * r1 成功（narrative 超 200 字符演示列表截断，详情带 2 条因果链：已确认/待验证各一）。
 */
const researchReports: MockResearchDetail[] = [
  {
    id: 2,
    reportType: 'manual',
    direction: '中性',
    confidence: '低',
    horizon: '',
    evidenceJson: '[]',
    risksJson: '[]',
    narrative: '',
    rawJson: '{}',
    verifyResult: '',
    error: 'LLM 响应超时，本次研报失败',
    roundId: '', // 空串 = 无关联，演示「该研报无工具调用记录」降级
    time: new Date(Date.now() - 6 * 3600_000).toISOString(),
    evidence: [],
    risks: [],
    raw: {},
    causalLinks: [],
  },
  {
    id: 1,
    reportType: 'asia_open',
    direction: '偏多',
    confidence: '中',
    horizon: '24h',
    evidenceJson:
      '[{"point":"美国 6 月 CPI 同比 3.0%，低于预期 3.1%","source":"金十日历"},{"point":"BTC 现货 ETF 连续三日净流入","source":"律动快讯"},{"point":"资金费率维持中性，未见过热","source":"Coinglass"}]',
    risksJson: '["美联储官员讲话偏鹰或压制风险偏好","亚盘流动性偏薄，波动易被放大"]',
    narrative:
      '亚盘时段宏观面偏多：美国 6 月 CPI 同比回落至 3.0%，低于市场预期的 3.1%，美元指数走弱，实际利率下行，风险资产偏好修复。资金面同样配合：BTC 现货 ETF 连续三日净流入，链上稳定币供应量回升，显示增量资金入场。衍生品侧资金费率维持中性，未见过热迹象，多头拥挤度可控。综合判断未来 24 小时偏多概率较大，但需警惕美联储官员鹰派讲话带来的短线扰动；亚盘流动性偏薄时段波动易被放大，建议控制仓位、避免追高。',
    rawJson:
      '{"direction":"偏多","confidence":"中","horizon":"24h","narrative":"（同 narrative 字段，此处从略）"}',
    verifyResult: '',
    error: '',
    roundId: 'rs-mock-1', // 演示工具链内嵌：mock getRound 对未知 roundId 回退通用详情
    time: new Date(Date.now() - 30 * 3600_000).toISOString(),
    evidence: [
      { point: '美国 6 月 CPI 同比 3.0%，低于预期 3.1%', source: '金十日历' },
      { point: 'BTC 现货 ETF 连续三日净流入', source: '律动快讯' },
      { point: '资金费率维持中性，未见过热', source: 'Coinglass' },
    ],
    risks: ['美联储官员讲话偏鹰或压制风险偏好', '亚盘流动性偏薄，波动易被放大'],
    raw: { direction: '偏多', confidence: '中', horizon: '24h' },
    causalLinks: [
      {
        id: 1,
        reportId: 1,
        chain: [
          { node: '美国 6 月 CPI 同比回落至 3.0%', kind: '事件', timeline_id: 1287 },
          { node: '美元指数走弱、实际利率下行', kind: '市场反应' },
          { node: '风险资产偏好修复，BTC 获增量资金流入', kind: '标的结论' },
        ],
        confidence: 0.72,
        evidence: ['金十日历：CPI 公布值 3.0% 低于预期 3.1%', '律动快讯：BTC ETF 单日净流入 2.1 亿美元'],
        status: 'verified',
        brokenAt: null,
        topic: 'CPI',
        supersedesId: null,
        awaitVerification: false,
        time: new Date(Date.now() - 30 * 3600_000).toISOString(),
      },
      {
        id: 3,
        reportId: 1,
        chain: [
          { node: '美联储官员鹰派讲话', kind: '事件', timeline_id: 1290 },
          { node: '降息预期降温', kind: '推断' },
          { node: '加密市场短线承压', kind: '市场反应' },
        ],
        confidence: 0.5,
        evidence: ['金十快讯：鲍威尔称「不急于降息」'],
        status: 'superseded',
        brokenAt: null,
        topic: '美联储',
        supersedesId: null,
        awaitVerification: true,
        time: new Date(Date.now() - 30 * 3600_000).toISOString(),
      },
      {
        id: 2,
        reportId: 1,
        chain: [
          { node: '美联储官员鹰派讲话', kind: '事件', timeline_id: 1290 },
          { node: '降息预期降温', kind: '推断' },
          { node: '加密市场短线承压回落', kind: '标的结论' },
        ],
        confidence: 0.55,
        evidence: ['金十快讯：鲍威尔称「不急于降息」', 'CME FedWatch：9 月降息概率降至 41%'],
        status: 'pending',
        brokenAt: null,
        topic: '美联储',
        supersedesId: 3,
        awaitVerification: true,
        time: new Date(Date.now() - 29 * 3600_000).toISOString(),
      },
    ],
  },
]

/** 手动研报：立即出一份成功研报（最新在前），演示成功后列表刷新。 */
function runMockResearch(reportType: string, hours: number): number {
  const newId = Math.max(0, ...researchReports.map((r) => r.id)) + 1
  const now = new Date().toISOString()
  researchReports.unshift({
    id: newId,
    reportType,
    direction: '中性',
    confidence: '低',
    horizon: `${hours}h`,
    evidenceJson:
      '[{"point":"宏观数据与消息面整体平淡","source":"金十日历"},{"point":"波动率处于近月低位","source":"Coinglass"}]',
    risksJson: '["临近美盘开盘，事件驱动风险上升"]',
    narrative:
      '最近 24 小时宏观数据与消息面整体平淡：无重磅经济数据公布，美联储官员讲话缺席，地缘政治无新增扰动。波动率处于近月低位，资金费率中性，ETF 资金流向平稳。综合判断未来 24 小时方向中性，建议观望等待更清晰的事件驱动。',
    rawJson: '{"direction":"中性","confidence":"低","horizon":"24h"}',
    verifyResult: '',
    error: '',
    roundId: `rs-mock-${newId}`, // 与下方 runResearch 返回的 roundId 自洽，演示新研报内嵌工具链
    time: now,
    evidence: [
      { point: '宏观数据与消息面整体平淡', source: '金十日历' },
      { point: '波动率处于近月低位', source: 'Coinglass' },
    ],
    risks: ['临近美盘开盘，事件驱动风险上升'],
    raw: { direction: '中性', confidence: '低', horizon: `${hours}h` },
    causalLinks: [],
  })
  return newId
}

/**
 * 创建研报 mock 域：返回 4 个 ApiClient 方法。
 * reply 由 mock.ts 注入，本模块不反向依赖 mock.ts（避免循环导入）。
 */
export function createResearchMock(reply: <T>(value: T) => Promise<T>) {
  const handlers: ResearchMockHandlers = {
    getResearchReports: (offset, limit) =>
      // 与后端契约一致：列表项 narrative 截断 200 字符、只给摘要字段，详情端点给全文与已解析对象
      reply({
        items: researchReports.slice(offset, offset + limit).map((r) => ({
          id: r.id,
          reportType: r.reportType,
          direction: r.direction,
          confidence: r.confidence,
          horizon: r.horizon,
          evidenceJson: r.evidenceJson,
          risksJson: r.risksJson,
          narrative: r.narrative.slice(0, 200),
          rawJson: r.rawJson,
          verifyResult: r.verifyResult,
          error: r.error,
          roundId: r.roundId,
          time: r.time,
        })),
        total: researchReports.length,
      }),
    getResearchReport: (id) => {
      const report = researchReports.find((r) => r.id === id)
      if (!report) return Promise.reject(new ApiError(404, `研报不存在: ${id}`))
      // 内部数据与后端同形状（evidence 为对象数组），返回前经 http 同一适配逻辑转展示串
      const detail = structuredClone(report)
      return reply({ ...detail, evidence: adaptEvidenceList(detail.evidence) })
    },
    runResearch: (reportType = 'manual', hours = 24) => {
      const newId = runMockResearch(reportType, hours)
      return reply({
        started: true,
        ok: true,
        reportId: newId,
        roundId: `rs-mock-${newId}`,
        direction: '中性',
        confidence: '低',
        error: '',
      })
    },
    // 恒无进行中的研报轮：进度条保持隐藏（与 getReviewLive 的「无轮次」契约同形）
    getResearchLive: () => reply({ round: null, tool_calls: [] }),
  }

  return { handlers }
}
