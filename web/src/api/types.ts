/**
 * API 契约类型定义：与后端 FastAPI（src/server/）的 REST / WS 接口一一对应。
 * 后端未就绪时由 mock.ts 提供同接口的假数据实现。
 */

/** 运行状态：GET /api/status */
export interface StatusInfo {
  mode: string // 运行模式：paper / testnet / live
  uptime_seconds: number // 已运行秒数
  kill_switch: boolean // 紧急停止开关（true 时拒绝一切交易）
  llm_provider: string // LLM 提供商
  llm_model: string // LLM 模型名
}

/** 账户概览：GET /api/account */
export interface AccountInfo {
  equity: number // 账户权益（USDT）
  available: number // 可用余额（USDT）
  unrealised_pnl: number // 未实现盈亏（USDT）
}

/** 持仓：GET /api/positions */
export interface Position {
  contract: string // 合约名，如 BTC_USDT
  size: number // 持仓张数，正多负空
  entry_price: number // 开仓均价
  mark_price: number // 标记价格
  leverage: number // 杠杆倍数
  unrealised_pnl: number // 未实现盈亏
  liq_price: number // 预估强平价
}

/** 决策轮摘要：GET /api/rounds?offset=&limit= */
export interface RoundSummary {
  round_id: string // 决策轮 ID
  started_at: string // 开始时间（ISO 字符串）
  wake_source: string // 唤醒来源（定时唤醒 / 价格触发 / 启动）
  summary: string // 本轮结论摘要
  pnl_after: number // 本轮结束后的累计盈亏
}

/** 工具调用记录（审计详情内嵌） */
export interface ToolCall {
  seq: number // 调用序号
  tool: string // 工具名
  args: Record<string, unknown> // 调用入参
  risk_verdict: string // 风控判定：allow / deny
  risk_reason: string // 风控理由（deny 时给出）
  result: string // 执行结果摘要
  duration_ms: number // 耗时（毫秒）
}

/** 决策轮审计详情：GET /api/rounds/{round_id} */
export interface RoundDetail {
  round_id: string
  prompt_snapshot: string // 完整 prompt 快照
  llm_raw: string // LLM 原始输出
  tool_calls: ToolCall[] // 工具调用链
}

/** 成交记录：GET /api/trades */
export interface Trade {
  time: string // 成交时间（ISO 字符串）
  contract: string // 合约名
  size: number // 成交张数，正买负卖
  price: number // 成交价
  fee: number // 手续费
  pnl: number // 已实现盈亏
}

/** 权益曲线点：GET /api/equity */
export interface EquityPoint {
  time: string // 时间（ISO 字符串）
  equity: number // 账户权益
}

/** 策略笔记：GET /api/notes */
export interface Note {
  time: string
  content: string
}

/** 风控参数（AppConfig.risk 子集，与 config.yaml 对齐） */
export interface RiskConfig {
  max_position_pct: number // 单仓名义价值/权益 上限（0-1]
  max_total_position_pct: number // 总仓名义价值/权益 上限（0-1]
  max_leverage: number // 杠杆上限（1-100 整数）
  daily_loss_limit: number // 日亏损锁仓阈值（0-1]
  max_orders_per_day: number // 日下单上限（整数）
  max_deviation: number // 委托价偏离标记价上限（0-1]
  kill_switch: boolean // 总开关（此处只读回显，操作走 /api/kill_switch）
}

/** 可编辑配置：GET/PUT /api/config（与 config.yaml 的可编辑子集对齐） */
export interface AppConfig {
  mode: string // 运行模式
  llm: {
    provider: string // anthropic / openai_compat
    model: string
    max_tokens: number
    openai_base_url: string
    max_consecutive_failures: number
  }
  risk: RiskConfig
  scheduler: {
    default_wake_minutes: number
    min_wake_minutes: number
    max_wake_minutes: number
  }
  notify: {
    telegram_enabled: boolean
  }
}

/** 交易对白名单：GET/PUT /api/watchlist */
export interface Watchlist {
  settle: string
  contracts: string[]
}

/** 密钥配置状态：GET /api/secrets/status（只返回布尔，永不返回明文） */
export interface SecretsStatus {
  gate_key: boolean // 交易所 API key 是否已配置
  llm_key: boolean // LLM key 是否已配置
  telegram: boolean // Telegram token 是否已配置
}

/** kill_switch 操作响应：POST /api/kill_switch */
export interface KillSwitchResult {
  kill_switch: boolean
}

/** WS 推送消息：/ws → {type, data} */
export type WsMessage =
  | { type: 'round'; data: RoundSummary }
  | { type: 'trade'; data: Trade }
  | { type: 'position'; data: Position }
  | { type: 'ticker'; data: { contract: string; last: number } }

/** REST 客户端统一接口（http.ts 真实实现 / mock.ts 假数据实现） */
export interface ApiClient {
  getStatus(): Promise<StatusInfo>
  getAccount(): Promise<AccountInfo>
  getPositions(): Promise<Position[]>
  getRounds(offset: number, limit: number): Promise<RoundSummary[]>
  getRound(roundId: string): Promise<RoundDetail>
  getTrades(): Promise<Trade[]>
  getEquity(): Promise<EquityPoint[]>
  getNotes(): Promise<Note[]>
  getConfig(): Promise<AppConfig>
  putConfig(config: AppConfig): Promise<AppConfig>
  getStrategy(): Promise<string>
  putStrategy(content: string): Promise<string>
  getWatchlist(): Promise<Watchlist>
  putWatchlist(list: Watchlist): Promise<Watchlist>
  getSecretsStatus(): Promise<SecretsStatus>
  setKillSwitch(enabled: boolean): Promise<KillSwitchResult>
}
