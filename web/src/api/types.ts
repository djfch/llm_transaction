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
  llm_configured: boolean // LLM API Key 是否已配置（false 时自动决策暂停）
  agent_running: boolean // 交易 Agent 是否在运行
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
  pnl_after?: number // 本轮结束后的累计盈亏（后端暂无此口径，缺失时显示 '-'）
}

/** 工具调用记录（审计详情内嵌） */
export interface ToolCall {
  seq: number // 调用序号
  tool: string // 工具名
  args: Record<string, unknown> | string // 调用入参（已解析为对象，也可能是字符串）
  risk_verdict: string // 风控判定：allow / deny
  risk_reason: string // 风控理由（deny 时给出）
  result: string | Record<string, unknown> // 执行结果（已解析为对象，也可能是字符串）
  duration_ms: number // 耗时（毫秒）
}

/** 决策轮审计详情：GET /api/rounds/{round_id} */
export interface RoundDetail {
  round_id: string
  prompt_snapshot: string // 完整 prompt 快照
  llm_raw: string // LLM 原始输出
  tool_calls: ToolCall[] // 工具调用链
}

/** 实时决策轮快照：GET /api/agent/live 的 round 字段 */
export interface AgentLiveRound {
  round_id: string // 决策轮 ID
  wake_source: string // 唤醒来源（定时唤醒 / 价格触发 / 启动）
  prompt_md5: string // system prompt 的 md5
  prompt_snapshot: string // 完整 prompt 快照
  context_snapshot: string // 本轮上下文快照
  llm_raw: string // LLM 原始输出（进行中为空串）
  started_at: number // 开始时间（Unix 秒）
  ended_at: number | null // 结束时间（Unix 秒，进行中为 null）
  error: string // 本轮错误信息（空串表示无错误）
}

/** 实时决策状态：GET /api/agent/live（无轮次时 round 为 null、tool_calls 为空） */
export interface AgentLiveState {
  in_round: boolean // agent 是否正在决策
  round: AgentLiveRound | null // 当前轮（进行中）或上一轮；从未决策时为 null
  tool_calls: ToolCall[] // 本轮已执行的工具调用（进行中实时追加）
}

/** 成交记录：GET /api/trades（created_at 已在 http 层适配为 time） */
export interface Trade {
  id: number // 成交 ID
  time: string // 成交时间（ISO 字符串，由后端 created_at(Unix秒) 适配而来）
  contract: string // 合约名
  size: number // 成交张数，正买负卖
  price: number // 成交价
  fee: number // 手续费
  pnl: number // 已实现盈亏
  source: string // 来源：llm_open / llm_close / user_close / liquidation / tpsl_close / ''
}

/** 成交记录分页结果：GET /api/trades */
export interface TradesPageResult {
  items: Trade[]
  total: number // 符合筛选条件的总笔数
  offset: number
  limit: number
}

/** K 线数据点：GET /api/candles（t 为 Unix 秒，价格字段为数字） */
export interface Candle {
  t: number // 开盘时间（Unix 秒）
  o: number // 开盘价
  h: number // 最高价
  l: number // 最低价
  c: number // 收盘价
  v: number // 成交量
}

/** 手动平仓结果：POST /api/positions/{contract}/close */
export interface ClosePositionResult {
  contract: string
  status: string // 平仓状态
  fill_price: number // 成交均价
  text: string // 结果描述文本
}

/** paper 模式权益设置结果：POST /api/paper/reset */
export interface PaperResetResult {
  equity: number
}

/** Agent 启停结果：POST /api/agent/start、POST /api/agent/stop */
export interface AgentStateResult {
  agent_running: boolean
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

/** PUT /api/config 响应（与后端逐字对齐）：基础 {saved, needs_restart}，
 * 仅 llm 热键（provider/model/max_tokens/openai_base_url）实际变更时追加 llm 两键 */
export interface PutConfigResult {
  saved: boolean
  needs_restart: string[] // 须重启才生效的变更字段
  llm_configured?: boolean // 仅 llm 热键变更时返回：热重建后 LLM 是否可用
  llm_error?: string // 仅 llm 热键变更时返回：provider 重建错误（空串 = 正常）
}

/** 密钥配置状态：GET /api/secrets/status（只返回布尔，永不返回明文） */
export interface SecretsStatus {
  gate_key: boolean // 交易所 API key 是否已配置
  llm_key: boolean // LLM key 是否已配置
  telegram: boolean // Telegram token 是否已配置
}

/** LLM 密钥保存请求体：POST /api/secrets（空串 = 不改动该项，发送前剔除） */
export interface SetSecretsBody {
  anthropic_api_key?: string // Anthropic API Key（非空才发送）
  openai_api_key?: string // OpenAI 兼容接口 Key（非空才发送）
}

/** LLM 密钥保存结果：POST /api/secrets 响应（永不含密钥明文） */
export interface SetSecretsResult {
  saved: boolean // 是否已写入服务器 .env
  llm_configured: boolean // 保存后 LLM 是否已配置可用
  error: string // 错误信息（如 provider 重建失败，空串 = 正常）
}

/** kill_switch 操作响应：POST /api/kill_switch */
export interface KillSwitchResult {
  kill_switch: boolean
}

/**
 * WS 推送消息：/ws → {type, data}
 * 注意（一期实际契约）：后端当前只广播 {type:'round'} 与 hello；trade/position/ticker 为预留类型，
 * 后端就绪前其 data 形态不作保证，消费 payload 前需按后端实际推送适配。
 */
export type WsMessage =
  | { type: 'round_start'; data: { wake_source: string } }
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
  getAgentLive(): Promise<AgentLiveState>
  getTrades(offset: number, limit: number, contract?: string): Promise<TradesPageResult>
  getCandles(contract: string, interval: string, limit?: number): Promise<Candle[]>
  closePosition(contract: string): Promise<ClosePositionResult>
  resetPaperEquity(equity: number): Promise<PaperResetResult>
  startAgent(): Promise<AgentStateResult>
  stopAgent(): Promise<AgentStateResult>
  getEquity(): Promise<EquityPoint[]>
  getNotes(): Promise<Note[]>
  getConfig(): Promise<AppConfig>
  putConfig(config: AppConfig): Promise<PutConfigResult>
  getStrategy(): Promise<string>
  putStrategy(content: string): Promise<string>
  getWatchlist(): Promise<Watchlist>
  putWatchlist(list: Watchlist): Promise<Watchlist>
  getSecretsStatus(): Promise<SecretsStatus>
  setSecrets(body: SetSecretsBody): Promise<SetSecretsResult>
  setKillSwitch(enabled: boolean): Promise<KillSwitchResult>
}
