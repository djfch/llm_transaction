/**
 * 真实后端实现：按契约请求 FastAPI（开发环境经 vite proxy 转发到 127.0.0.1:17577）。
 * 后端原始字段（items/created_at/数字字符串）→ 前端类型的适配集中在在本文件，页面不感知。
 */
import type {
  AccountInfo,
  AgentLiveState,
  AgentStateResult,
  ApiClient,
  AppConfig,
  Candle,
  CancelOpenOrderResult,
  ClosePositionResult,
  CredentialCreateBody,
  CredentialMutationResult,
  CredentialUpdateBody,
  DailyStats,
  EquitySeries,
  IndicatorConfig,
  IndicatorSeriesResponse,
  KillSwitchResult,
  NotesPageResult,
  PaperResetResult,
  OpenOrder,
  PriceAlert,
  Position,
  PortfolioSnapshot,
  PutConfigResult,
  ReviewReport,
  ReviewReportsPage,
  ReviewReportSummary,
  RollbackResult,
  RoundDetail,
  RoundsPageResult,
  RunReviewResult,
  SecretsStatus,
  SetSecretsBody,
  SetSecretsResult,
  StatusInfo,
  StrategyVersion,
  StrategyVersionDetail,
  Trade,
  TradePlan,
  TradesPageResult,
  Watchlist,
} from './types'

const BASE = '/api'

/** 带状态码与 detail 的 API 错误（如 422 风控拒绝、409 非 paper 模式） */
export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly detail: string,
  ) {
    super(detail)
    this.name = 'ApiError'
  }
}

/** 把非 2xx 响应体转成 Error：FastAPI 的 {detail} 提取为 ApiError，其余为通用 Error */
function toApiError(status: number, body: string): Error {
  try {
    const parsed = JSON.parse(body) as { detail?: unknown }
    if (typeof parsed.detail === 'string') return new ApiError(status, parsed.detail)
    if (Array.isArray(parsed.detail)) {
      // FastAPI 422：detail 为 [{loc, msg, type}...]，提取各项 msg 拼成可读串，避免整段 JSON 进 message
      const msgs = parsed.detail
        .map((item) =>
          item !== null && typeof item === 'object' && typeof (item as { msg?: unknown }).msg === 'string'
            ? (item as { msg: string }).msg
            : null,
        )
        .filter((m): m is string => m !== null)
      if (msgs.length > 0) return new ApiError(status, msgs.join('；'))
    }
  } catch {
    // 响应体非 JSON，走通用错误
  }
  return new Error(`请求失败 ${status}: ${body}`)
}

/** JSON 请求封装：非 2xx 抛错（{detail} 提取为 ApiError，便于页面展示风控原因） */
async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  if (!res.ok) throw toApiError(res.status, await res.text())
  return (await res.json()) as T
}

/** 文本请求封装：/api/strategy 按纯文本收发 */
async function requestText(path: string, init?: RequestInit): Promise<string> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'text/plain; charset=utf-8' },
    ...init,
  })
  if (!res.ok) throw toApiError(res.status, await res.text())
  return res.text()
}

/** 后端原始成交记录：数字字段可能是数字字符串，时间为 created_at(Unix秒) */
interface RawTrade {
  id: number
  round_id: string
  mode: string
  contract: string
  size: number | string
  price: number | string
  fee: number | string
  pnl: number | string
  source: string
  created_at: number
}

/** 后端 Trade → 前端 Trade：created_at(Unix秒) 转 ISO time，数字字符串统一转 number */
function adaptTrade(raw: RawTrade): Trade {
  return {
    id: raw.id,
    round_id: raw.round_id ?? '',
    time: new Date(raw.created_at * 1000).toISOString(),
    contract: raw.contract,
    size: Number(raw.size),
    price: Number(raw.price),
    fee: Number(raw.fee),
    pnl: Number(raw.pnl),
    source: raw.source ?? '',
  }
}

/** 后端原始账户：数字字段可能是数字字符串（Decimal 序列化） */
type RawAccount = { [K in keyof AccountInfo]: number | string }

/** 后端 Account → 前端 AccountInfo：数字字符串统一转 number */
function adaptAccount(raw: RawAccount): AccountInfo {
  return {
    equity: Number(raw.equity),
    available: Number(raw.available),
    unrealised_pnl: Number(raw.unrealised_pnl),
  }
}

/** 后端原始持仓：数字字段可能是数字字符串 */
type RawPosition = Omit<{ [K in keyof Position]: number | string }, 'stop_loss_price' | 'take_profit_price'> & {
  stop_loss_price: number | string | null
  take_profit_price: number | string | null
}

/** 后端 Position → 前端 Position：数字字符串统一转 number */
function adaptPosition(raw: RawPosition): Position {
  return {
    contract: String(raw.contract),
    size: Number(raw.size),
    entry_price: Number(raw.entry_price),
    mark_price: Number(raw.mark_price),
    leverage: Number(raw.leverage),
    margin: Number(raw.margin),
    unrealised_pnl: Number(raw.unrealised_pnl),
    liq_price: Number(raw.liq_price),
    stop_loss_price: raw.stop_loss_price == null ? null : Number(raw.stop_loss_price),
    take_profit_price: raw.take_profit_price == null ? null : Number(raw.take_profit_price),
  }
}
type RawOpenOrder = Omit<
  OpenOrder,
  'size' | 'left' | 'price' | 'stop_loss_price' | 'take_profit_price'
> & {
  size: number | string
  left: number | string
  price: number | string
  stop_loss_price?: number | string | null
  take_profit_price?: number | string | null
}

/** 将后端 Decimal 字符串订单快照转换为前端可渲染的数字模型。 */
function adaptOpenOrder(raw: RawOpenOrder): OpenOrder {
  return {
    id: String(raw.id),
    contract: String(raw.contract),
    size: Number(raw.size),
    left: Number(raw.left),
    price: Number(raw.price),
    tif: String(raw.tif),
    reduce_only: Boolean(raw.reduce_only),
    status: String(raw.status),
    stop_loss_price: raw.stop_loss_price == null ? null : Number(raw.stop_loss_price),
    take_profit_price: raw.take_profit_price == null ? null : Number(raw.take_profit_price),
  }
}

/** 后端 /api/alerts：price 为 Decimal 序列化值（number 或数字字符串），created_at 为 Unix 秒。 */
type RawPriceAlert = Omit<PriceAlert, 'price' | 'time'> & {
  price: number | string
  created_at: number | string
}

/** 后端价格唤醒 → 前端 PriceAlert：price 转 number，created_at(Unix秒) 转 ISO time（与 Trade/Note 同约定）。 */
function adaptPriceAlert(raw: RawPriceAlert): PriceAlert {
  return {
    id: Number(raw.id),
    contract: String(raw.contract),
    direction: raw.direction,
    price: Number(raw.price),
    time: new Date(Number(raw.created_at) * 1000).toISOString(),
  }
}

/** 后端 /api/equity：{initial_equity, baseline_source, points:[{t(Unix秒), equity}]} */
interface RawEquity {
  initial_equity: number
  baseline_source: string
  points: Array<{ t: number; equity: number }>
}

/** 后端 equity 响应 → 前端 EquityPoint[]（取 points，t 转 ISO time） */
function adaptEquity(raw: RawEquity): EquitySeries {
  return {
    initialEquity: Number(raw.initial_equity),
    baselineSource: raw.baseline_source,
    points: raw.points.map((p) => ({
      time: new Date(p.t * 1000).toISOString(),
      equity: Number(p.equity),
    })),
  }
}

interface RawPortfolio {
  as_of: number
  account: RawAccount
  positions: RawPosition[]
}

function adaptPortfolio(raw: RawPortfolio): PortfolioSnapshot {
  return {
    asOf: new Date(raw.as_of * 1000).toISOString(),
    account: adaptAccount(raw.account),
    positions: raw.positions.map(adaptPosition),
  }
}

/** 后端 /api/notes：标准分页壳 + created_at(Unix秒) 的笔记项。 */
interface RawNotesPage {
  items: Array<{ created_at: number; content: string; round_id?: string }>
  total: number
  offset: number
  limit: number
}

/** 后端 notes 分页响应 → 前端 NotesPageResult：时间转 ISO，并防御性保持最新在前。 */
function adaptNotes(raw: RawNotesPage): NotesPageResult {
  return {
    ...raw,
    items: [...raw.items].sort((a, b) => b.created_at - a.created_at).map((n) => ({
      time: new Date(n.created_at * 1000).toISOString(),
      content: n.content,
      round_id: n.round_id ?? '',
    })),
  }
}

/** 后端 /api/rounds 列表项：Decisions 行 + audit 摘要 + 归属笔记引文（note 可空） */
interface RawRoundItem {
  round_id: string
  wake_source: string
  context_summary: string
  created_at: number
  strategy_md5?: string // 契约恒为 string；可选仅防御历史后端
  note?: { content: string; created_at: number } | null // 契约恒带此键；可选仅防御历史后端
}

interface RawRoundsPage {
  items: RawRoundItem[]
  total: number
  offset: number
  limit: number
}

/** 后端 rounds 分页响应 → 前端 RoundsPageResult，摘要字段转换集中在此处。 */
function adaptRounds(raw: RawRoundsPage): RoundsPageResult {
  return {
    ...raw,
    items: raw.items.map((r) => ({
      round_id: r.round_id,
      started_at: new Date(r.created_at * 1000).toISOString(),
      wake_source: r.wake_source,
      summary: r.context_summary,
      strategyMd5: r.strategy_md5 ?? '',
      pnl_after: undefined,
      note: r.note
        ? { content: r.note.content, time: new Date(r.note.created_at * 1000).toISOString() }
        : null,
    })),
  }
}

/** 后端 /api/rounds/{id} 详情：契约含 strategy_md5，其余键与前端 RoundDetail 一致 */
type RawRoundDetail = Omit<RoundDetail, 'strategyMd5'> & { strategy_md5?: string }

/** 后端 round 详情 → 前端 RoundDetail：strategy_md5 适配为 strategyMd5 */
function adaptRoundDetail(raw: RawRoundDetail): RoundDetail {
  return {
    round_id: raw.round_id,
    prompt_snapshot: raw.prompt_snapshot,
    llm_raw: raw.llm_raw,
    tool_calls: raw.tool_calls,
    strategyMd5: raw.strategy_md5 ?? '',
  }
}

/** 后端 /api/daily_stats：数值字段可能是数字字符串（Decimal 序列化） */
type RawDailyStats = { [K in keyof DailyStats]: number | string }

/** 后端 daily_stats 响应 → 前端 DailyStats：数字字符串统一转 number */
function adaptDailyStats(raw: RawDailyStats): DailyStats {
  return {
    realized_pnl: Number(raw.realized_pnl),
    orders_today: Number(raw.orders_today),
    max_orders_per_day: Number(raw.max_orders_per_day),
  }
}

/** 后端复盘报告原始项（列表/详情同 10 键，仅 report_md 长度不同） */
interface RawReviewReport {
  id: number
  period_start: number
  period_end: number
  stats_json: string
  report_md: string
  strategy_action: string
  new_version_id: number | null
  error: string
  round_id: string
  created_at: number
}

/**
 * 后端复盘报告 → 前端 ReviewReportSummary：Unix 秒时间统一转 ISO；
 * stats_json 保留原始字符串不解析（口径由后端 stats.py 冻结，展示方自行 JSON.parse
 * 并按字段缺失降级，适配层不为统计字段建类型以免双端口径漂移）。
 */
function adaptReviewReport(raw: RawReviewReport): ReviewReportSummary {
  return {
    id: raw.id,
    periodStart: new Date(raw.period_start * 1000).toISOString(),
    periodEnd: new Date(raw.period_end * 1000).toISOString(),
    statsJson: raw.stats_json,
    reportMd: raw.report_md,
    strategyAction: raw.strategy_action === 'rewrite' ? 'rewrite' : 'none',
    newVersionId: raw.new_version_id ?? null,
    error: raw.error ?? '',
    roundId: raw.round_id ?? '', // 老报告/异常缺省降级为空串（契约保证返回，?? 为防御）
    time: new Date(raw.created_at * 1000).toISOString(),
  }
}

/** 后端 POST /api/review/run 原始响应（成功/失败的键集合不同，全部可选防御） */
interface RawRunReview {
  started: boolean
  ok?: boolean
  report_id?: number
  round_id?: string
  strategy_action?: string
  new_version_id?: number | null
  error?: string
}

/** 后端 run 响应 → 前端 RunReviewResult：snake_case 转 camelCase */
function adaptRunReview(raw: RawRunReview): RunReviewResult {
  return {
    started: Boolean(raw.started),
    ok: raw.ok,
    reportId: raw.report_id,
    roundId: raw.round_id,
    strategyAction: raw.strategy_action,
    newVersionId: raw.new_version_id ?? null,
    error: raw.error ?? '',
  }
}

/** 后端策略版本原始项（列表无 content，详情有） */
interface RawStrategyVersion {
  id: number
  md5: string
  created_by: string
  reason: string
  report_id: number | null
  created_at: number
  content?: string
}

/** 后端策略版本 → 前端 StrategyVersion：created_at(Unix秒) 转 ISO time */
function adaptStrategyVersion(raw: RawStrategyVersion): StrategyVersion {
  return {
    id: raw.id,
    md5: raw.md5,
    createdBy: raw.created_by,
    reason: raw.reason,
    reportId: raw.report_id ?? null,
    time: new Date(raw.created_at * 1000).toISOString(),
  }
}

/** 后端回滚响应 → 前端 RollbackResult */
function adaptRollback(raw: { rolled_back_to: number; version: number }): RollbackResult {
  return { rolledBackTo: Number(raw.rolled_back_to), version: Number(raw.version) }
}

/** GET /api/trades 适配：{items,total,offset,limit} + items 内字段转换 */
async function fetchTrades(offset: number, limit: number, contract?: string): Promise<TradesPageResult> {
  const qs = new URLSearchParams({ offset: String(offset), limit: String(limit) })
  if (contract) qs.set('contract', contract)
  const raw = await request<{ items: RawTrade[]; total: number; offset: number; limit: number }>(
    `/trades?${qs.toString()}`,
  )
  return { ...raw, items: raw.items.map(adaptTrade) }
}

/** GET /api/rounds 适配：使用 URLSearchParams 固化分页参数并转换摘要字段。 */
async function fetchRounds(offset: number, limit: number): Promise<RoundsPageResult> {
  const qs = new URLSearchParams({ offset: String(offset), limit: String(limit) })
  return adaptRounds(await request<RawRoundsPage>(`/rounds?${qs.toString()}`))
}

/** GET /api/notes 适配：默认返回前 20 条笔记，同时返回分页元数据。 */
async function fetchNotes(offset = 0, limit = 20): Promise<NotesPageResult> {
  const qs = new URLSearchParams({ offset: String(offset), limit: String(limit) })
  return adaptNotes(await request<RawNotesPage>(`/notes?${qs.toString()}`))
}

/** 后端 /api/plan：{content, round_id, updated_at(Unix秒|null)}。 */
interface RawTradePlan {
  content: string
  round_id: string
  updated_at: number | null
}

/** 后端 plan 响应 → 前端 TradePlan（updated_at 转 ISO time，无计划时 null）。 */
function adaptTradePlan(raw: RawTradePlan): TradePlan {
  return {
    content: raw.content ?? '',
    roundId: raw.round_id ?? '',
    updatedAt: raw.updated_at ? new Date(raw.updated_at * 1000).toISOString() : null,
  }
}

/** GET /api/review/reports 适配：{items,total}（后端不回显 offset/limit）+ items 字段转换 */
async function fetchReviewReports(offset: number, limit: number): Promise<ReviewReportsPage> {
  const qs = new URLSearchParams({ offset: String(offset), limit: String(limit) })
  const raw = await request<{ items: RawReviewReport[]; total: number }>(`/review/reports?${qs.toString()}`)
  return { items: raw.items.map(adaptReviewReport), total: raw.total }
}

/** GET /api/candles 适配：取出 items 数组（字段名 t/o/h/l/c/v 与前端一致） */
async function fetchCandles(contract: string, interval: string, limit = 200): Promise<Candle[]> {
  const qs = new URLSearchParams({ contract, interval, limit: String(limit) })
  const raw = await request<{ items: Candle[] }>(`/candles?${qs.toString()}`)
  return raw.items
}

/** 后端原始指标点：value 为数字字符串或 null（Decimal 序列化），time 为 Unix 秒 */
interface RawIndicatorPoint {
  time: number
  value: number | string | null
}

/** 后端原始指标序列条目：kind 契约恒为 overlay/pane/scalar（可选仅防御历史后端） */
interface RawIndicatorEntry {
  label: string
  kind?: string
  fields?: Record<string, RawIndicatorPoint[]>
  current?: number | string | null
}

/** 后端指标 kind → 前端 IndicatorKind：未知值降级 pane（独立副图渲染，不污染主图） */
function adaptIndicatorKind(kind: string | undefined): 'overlay' | 'pane' | 'scalar' {
  if (kind === 'overlay' || kind === 'scalar') return kind
  return 'pane'
}

/** 后端指标点 → 前端：value 数字字符串转 number（null 保持），time 保持 Unix 秒 */
function adaptIndicatorPoint(raw: RawIndicatorPoint): { time: number; value: number | null } {
  return { time: Number(raw.time), value: raw.value == null ? null : Number(raw.value) }
}

/** 后端指标条目 → 前端 IndicatorSeriesEntry：fields 各序列逐点适配 */
function adaptIndicatorEntry(raw: RawIndicatorEntry): IndicatorSeriesResponse['series'][string] {
  const fields: Record<string, Array<{ time: number; value: number | null }>> = {}
  for (const [name, points] of Object.entries(raw.fields ?? {})) {
    fields[name] = (points ?? []).map(adaptIndicatorPoint)
  }
  return {
    label: raw.label ?? '',
    kind: adaptIndicatorKind(raw.kind),
    fields,
    current: raw.current == null ? null : Number(raw.current),
  }
}

/** GET /api/indicators/series 适配：keys 以逗号拼接；series 各条目数字字符串转 number */
async function fetchIndicatorSeries(
  contract: string,
  interval: string,
  keys: string[],
  limit = 200,
): Promise<IndicatorSeriesResponse> {
  const qs = new URLSearchParams({ contract, interval, limit: String(limit), keys: keys.join(',') })
  const raw = await request<{
    contract: string
    interval: string
    series: Record<string, RawIndicatorEntry>
  }>(`/indicators/series?${qs.toString()}`)
  const series: IndicatorSeriesResponse['series'] = {}
  for (const [key, entry] of Object.entries(raw.series ?? {})) {
    series[key] = adaptIndicatorEntry(entry)
  }
  return { contract: raw.contract, interval: raw.interval, series }
}

/** 后端 /api/indicator_config 原始项：kind 契约恒为 overlay/pane/scalar（可选仅防御） */
interface RawIndicatorAvailable {
  key: string
  label: string
  kind?: string
  fields?: string[]
}

/** GET /api/indicator_config 适配：kind 归一化，fields 缺省补空数组 */
async function fetchIndicatorConfig(): Promise<IndicatorConfig> {
  const raw = await request<{ shortlist: string[]; available: RawIndicatorAvailable[] }>('/indicator_config')
  return {
    shortlist: raw.shortlist ?? [],
    available: (raw.available ?? []).map((item) => ({
      key: String(item.key),
      label: item.label ?? '',
      kind: adaptIndicatorKind(item.kind),
      fields: item.fields ?? [],
    })),
  }
}

export const httpApi: ApiClient = {
  getStatus: () => request<StatusInfo>('/status'),
  getAccount: async () => adaptAccount(await request<RawAccount>('/account')),
  getPositions: async () =>
    (await request<RawPosition[]>('/positions')).map(adaptPosition),
  getPortfolio: async () => adaptPortfolio(await request<RawPortfolio>('/portfolio')),
  getOpenOrders: async () => (await request<RawOpenOrder[]>('/open_orders')).map(adaptOpenOrder),
  getAlerts: async () => (await request<RawPriceAlert[]>('/alerts')).map(adaptPriceAlert),
  getRounds: fetchRounds,
  getRound: async (roundId) =>
    adaptRoundDetail(await request<RawRoundDetail>(`/rounds/${encodeURIComponent(roundId)}`)),
  // 响应契约即最终形态（args/result 已解析、started_at 为 Unix 秒），无需适配
  getAgentLive: () => request<AgentLiveState>('/agent/live'),
  getTrades: fetchTrades,
  getCandles: fetchCandles,
  getIndicatorConfig: fetchIndicatorConfig,
  getIndicatorSeries: fetchIndicatorSeries,
  closePosition: (contract) =>
    request<ClosePositionResult>(`/positions/${encodeURIComponent(contract)}/close`, {
      method: 'POST',
    }),
  cancelOpenOrder: (contract, orderId) =>
    request<CancelOpenOrderResult>(`/orders/${encodeURIComponent(contract)}/${encodeURIComponent(orderId)}`, {
      method: 'DELETE',
    }),
  resetPaperEquity: (equity) =>
    request<PaperResetResult>('/paper/reset', { method: 'POST', body: JSON.stringify({ equity }) }),
  startAgent: () => request<AgentStateResult>('/agent/start', { method: 'POST' }),
  stopAgent: () => request<AgentStateResult>('/agent/stop', { method: 'POST' }),
  getEquity: async () => adaptEquity(await request<RawEquity>('/equity')),
  getNotes: fetchNotes,
  getPlan: async () => adaptTradePlan(await request<RawTradePlan>('/plan')),
  getDailyStats: async () => adaptDailyStats(await request<RawDailyStats>('/daily_stats')),
  getConfig: () => request<AppConfig>('/config'),
  putConfig: (config) =>
    request<PutConfigResult>('/config', { method: 'PUT', body: JSON.stringify(config) }),
  getStrategy: () => requestText('/strategy'),
  putStrategy: (content) => requestText('/strategy', { method: 'PUT', body: content }),
  getWatchlist: () => request<Watchlist>('/watchlist'),
  putWatchlist: (list) =>
    request<Watchlist>('/watchlist', { method: 'PUT', body: JSON.stringify(list) }),
  getSecretsStatus: () => request<SecretsStatus>('/secrets/status'),
  setSecrets: (body: SetSecretsBody) =>
    request<SetSecretsResult>('/secrets', { method: 'POST', body: JSON.stringify(body) }),
  createCredential: (body: CredentialCreateBody) =>
    request<CredentialMutationResult>('/credentials', { method: 'POST', body: JSON.stringify(body) }),
  updateCredential: (name: string, body: CredentialUpdateBody) =>
    request<CredentialMutationResult>(`/credentials/${encodeURIComponent(name)}`, {
      method: 'PUT',
      body: JSON.stringify(body),
    }),
  deleteCredential: (name: string) =>
    request<CredentialMutationResult>(`/credentials/${encodeURIComponent(name)}`, { method: 'DELETE' }),
  setKillSwitch: (enabled) =>
    request<KillSwitchResult>('/kill_switch', {
      method: 'POST',
      body: JSON.stringify({ enabled }),
    }),
  getReviewReports: fetchReviewReports,
  getReviewReport: async (id): Promise<ReviewReport> =>
    adaptReviewReport(await request<RawReviewReport>(`/review/reports/${id}`)),
  runReview: async () => adaptRunReview(await request<RawRunReview>('/review/run', { method: 'POST' })),
  getStrategyVersions: async () =>
    (await request<{ items: RawStrategyVersion[] }>('/strategy/versions')).items.map(adaptStrategyVersion),
  getStrategyVersion: async (id): Promise<StrategyVersionDetail> => {
    const raw = await request<RawStrategyVersion>(`/strategy/versions/${id}`)
    return { ...adaptStrategyVersion(raw), content: raw.content ?? '' }
  },
  getStrategyDiff: (fromId, toId) => {
    const qs = new URLSearchParams({ from: String(fromId), to: String(toId) })
    return requestText(`/strategy/diff?${qs.toString()}`)
  },
  rollbackStrategy: async (id) =>
    adaptRollback(
      await request<{ rolled_back_to: number; version: number }>(`/strategy/rollback/${id}`, { method: 'POST' }),
    ),
}
