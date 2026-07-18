/**
 * 真实后端实现：按契约请求 FastAPI（开发环境经 vite proxy 转发到 127.0.0.1:8080）。
 */
import type {
  AccountInfo,
  ApiClient,
  AppConfig,
  EquityPoint,
  KillSwitchResult,
  Note,
  Position,
  RoundDetail,
  RoundSummary,
  SecretsStatus,
  StatusInfo,
  Trade,
  Watchlist,
} from './types'

const BASE = '/api'

/** JSON 请求封装：非 2xx 抛错（带响应体便于排查） */
async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  if (!res.ok) {
    throw new Error(`请求失败 ${res.status}: ${await res.text()}`)
  }
  return (await res.json()) as T
}

/** 文本请求封装：/api/strategy 按纯文本收发 */
async function requestText(path: string, init?: RequestInit): Promise<string> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'text/plain; charset=utf-8' },
    ...init,
  })
  if (!res.ok) {
    throw new Error(`请求失败 ${res.status}: ${await res.text()}`)
  }
  return res.text()
}

export const httpApi: ApiClient = {
  getStatus: () => request<StatusInfo>('/status'),
  getAccount: () => request<AccountInfo>('/account'),
  getPositions: () => request<Position[]>('/positions'),
  getRounds: (offset, limit) => request<RoundSummary[]>(`/rounds?offset=${offset}&limit=${limit}`),
  getRound: (roundId) => request<RoundDetail>(`/rounds/${encodeURIComponent(roundId)}`),
  getTrades: () => request<Trade[]>('/trades'),
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
