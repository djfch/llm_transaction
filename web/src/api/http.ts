/**
 * 真实后端实现：按契约请求 FastAPI（开发环境经 vite proxy 转发到 127.0.0.1:8080）。
 * 后端原始字段（items/created_at/数字字符串）→ 前端类型的适配集中在在本文件，页面不感知。
 */
import type {
  AccountInfo,
  AgentStateResult,
  ApiClient,
  AppConfig,
  Candle,
  ClosePositionResult,
  EquityPoint,
  KillSwitchResult,
  Note,
  PaperResetResult,
  Position,
  RoundDetail,
  RoundSummary,
  SecretsStatus,
  StatusInfo,
  Trade,
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
    time: new Date(raw.created_at * 1000).toISOString(),
    contract: raw.contract,
    size: Number(raw.size),
    price: Number(raw.price),
    fee: Number(raw.fee),
    pnl: Number(raw.pnl),
    source: raw.source ?? '',
  }
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

/** GET /api/candles 适配：取出 items 数组（字段名 t/o/h/l/c/v 与前端一致） */
async function fetchCandles(contract: string, interval: string, limit = 200): Promise<Candle[]> {
  const qs = new URLSearchParams({ contract, interval, limit: String(limit) })
  const raw = await request<{ items: Candle[] }>(`/candles?${qs.toString()}`)
  return raw.items
}

export const httpApi: ApiClient = {
  getStatus: () => request<StatusInfo>('/status'),
  getAccount: () => request<AccountInfo>('/account'),
  getPositions: () => request<Position[]>('/positions'),
  getRounds: (offset, limit) => request<RoundSummary[]>(`/rounds?offset=${offset}&limit=${limit}`),
  getRound: (roundId) => request<RoundDetail>(`/rounds/${encodeURIComponent(roundId)}`),
  getTrades: fetchTrades,
  getCandles: fetchCandles,
  closePosition: (contract) =>
    request<ClosePositionResult>(`/positions/${encodeURIComponent(contract)}/close`, {
      method: 'POST',
    }),
  resetPaperEquity: (equity) =>
    request<PaperResetResult>('/paper/reset', { method: 'POST', body: JSON.stringify({ equity }) }),
  startAgent: () => request<AgentStateResult>('/agent/start', { method: 'POST' }),
  stopAgent: () => request<AgentStateResult>('/agent/stop', { method: 'POST' }),
  getEquity: () => request<EquityPoint[]>('/equity'),
  getNotes: () => request<Note[]>('/notes'),
  getConfig: () => request<AppConfig>('/config'),
  putConfig: (config) =>
    request<AppConfig>('/config', { method: 'PUT', body: JSON.stringify(config) }),
  getStrategy: () => requestText('/strategy'),
  putStrategy: (content) => requestText('/strategy', { method: 'PUT', body: content }),
  getWatchlist: () => request<Watchlist>('/watchlist'),
  putWatchlist: (list) =>
    request<Watchlist>('/watchlist', { method: 'PUT', body: JSON.stringify(list) }),
  getSecretsStatus: () => request<SecretsStatus>('/secrets/status'),
  setKillSwitch: (enabled) =>
    request<KillSwitchResult>('/kill_switch', {
      method: 'POST',
      body: JSON.stringify({ enabled }),
    }),
}
