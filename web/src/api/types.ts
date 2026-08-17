/**
 * API 契约类型定义：与后端 FastAPI（src/server/）的 REST / WS 接口一一对应。
 * 后端未就绪时由 mock.ts 提供同接口的假数据实现。
 */

/** 运行状态：GET /api/status */
export interface StatusInfo {
  mode: string // 运行模式：paper / testnet / live
  uptime_seconds: number // 已运行秒数
  kill_switch: boolean // 紧急停止开关（true 时拒绝一切交易）
  llm_credential_name: string // 决策 Agent 当前绑定的 LLM 凭证名
  llm_provider: string // LLM 提供商
  llm_model: string // LLM 模型名
  llm_thinking_effort: string // 思考强度：空串表示跟随模型默认
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
/** 账户与持仓在同一读取周期内生成的权威快照：GET /api/portfolio */
export interface PortfolioSnapshot {
  asOf: string
  account: AccountInfo
  positions: Position[]
}

export interface Position {
  contract: string // 合约名，如 BTC_USDT
  size: number // 持仓张数，正多负空
  entry_price: number // 开仓均价
  mark_price: number // 标记价格
  leverage: number // 杠杆倍数
  margin: number // 持仓保证金（USDT）
  unrealised_pnl: number // 未实现盈亏
  liq_price: number // 预估强平价
  stop_loss_price: number | null // 止损触发价；未设置为 null
  take_profit_price: number | null // 止盈触发价；未设置为 null
}

/** 未成交挂单的前端契约，字段与 GET /api/open_orders 一一对应。 */
export interface OpenOrder {
  id: string
  contract: string
  size: number
  left: number
  price: number
  tif: string
  reduce_only: boolean
  status: string
  stop_loss_price: number | null
  take_profit_price: number | null
}

/** LLM 设置的未触发价格唤醒：GET /api/alerts（触发后即从列表消失）。 */
export interface PriceAlert {
  id: number
  contract: string
  direction: 'above' | 'below' // above=价格上破触发价；below=下破触发价
  price: number // 触发价
  time: string // 设置时间（ISO 字符串，由适配层自 created_at(Unix秒) 转换）
}

/** 决策轮摘要：GET /api/rounds?offset=&limit= */
export interface RoundSummary {
  round_id: string // 决策轮 ID
  started_at: string // 开始时间（ISO 字符串）
  wake_source: string // 唤醒来源（定时唤醒 / 价格触发 / 启动）
  summary: string // 本轮结论摘要
  strategyMd5: string // 策略书原文 md5（空串 = 历史数据无关联），用于 join 策略版本
  pnl_after?: number // 本轮结束后的累计盈亏（后端暂无此口径，缺失时显示 '-'）
  note?: { content: string; time: string } | null // 归属笔记引文（随当前页下发，每轮最新一条；null/缺省 = 无归属）
}

/** 通用分页结果：items(当前页内容) 与 total(匹配总数) 由服务端一并返回。 */
export interface PageResult<T> {
  items: T[]
  total: number // 全部符合条件的记录数
  offset: number // 当前页首项的零基偏移量
  limit: number // 本次请求的单页条数
}

/** 决策轮分页结果：GET /api/rounds?offset=&limit=。 */
export type RoundsPageResult = PageResult<RoundSummary>

/** 工具调用记录（审计详情内嵌） */
export interface ToolCall {
  seq: number // 调用序号
  tool: string // 工具名
  args: Record<string, unknown> | string // 调用入参（已解析为对象，也可能是字符串）
  risk_verdict: string // 风控判定：allow / deny / 空串(未入风控：非交易工具，或交易工具参数校验失败)
  risk_reason: string // 风控理由（deny 时给出）
  result: string | Record<string, unknown> // 执行结果（已解析为对象，也可能是字符串）
  duration_ms: number // 耗时（毫秒）
}

/** 决策轮审计详情：GET /api/rounds/{round_id} */
export interface RoundDetail {
  round_id: string
  prompt_snapshot: string // 完整 prompt 快照
  context_snapshot: string // 首次发送给 LLM 的 user 上下文快照
  llm_raw: string // LLM 原始输出
  tool_calls: ToolCall[] // 工具调用链
  strategyMd5: string // 策略书原文 md5（空串 = 历史数据无关联）
}

/** 实时决策轮快照：GET /api/agent/live 的 round 字段 */
export interface AgentLiveRound {
  round_id: string // 决策轮 ID
  wake_source: string // 唤醒来源（定时唤醒 / 价格触发 / 启动）
  prompt_md5: string // system prompt 的 md5
  prompt_snapshot: string // 完整 prompt 快照
  context_snapshot: string // 本轮上下文快照
  llm_raw: string // 截至当前累计收到的 LLM 原始输出；尚未返回时为空串
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

/** 实时复盘轮快照：GET /api/review/live 的 round 字段（形状与 AgentLiveRound 一致并透传 snake_case，另带 strategy_md5） */
export interface ReviewLiveRound extends AgentLiveRound {
  strategy_md5: string // 复盘所依据策略书的 md5
}

/** 实时复盘状态：GET /api/review/live（无复盘轮时 round 为 null、tool_calls 为空；进行中 ended_at 为 null） */
export interface ReviewLive {
  round: ReviewLiveRound | null // 当前（进行中）或上一复盘轮；从未复盘时为 null
  tool_calls: ToolCall[] // 本轮已执行的复盘工具调用（进行中实时追加）
}

/** 成交记录：GET /api/trades（created_at 已在 http 层适配为 time） */
export interface Trade {
  id: number // 成交 ID
  round_id: string // 产生该成交的决策轮 ID（空串=无归属，如历史/未知来源）
  time: string // 成交时间（ISO 字符串，由后端 created_at(Unix秒) 适配而来）
  contract: string // 合约名
  size: number // 成交张数，正买负卖
  price: number // 成交价
  fee: number // 手续费
  pnl: number // 已实现盈亏
  source: string // 来源：llm_open / llm_close / user_close / liquidation / tpsl_close / ''
}

/** 成交记录分页结果：GET /api/trades。 */
export type TradesPageResult = PageResult<Trade>

/** K 线数据点：GET /api/candles（t 为 Unix 秒，价格字段为数字） */
export interface Candle {
  t: number // 开盘时间（Unix 秒）
  o: number // 开盘价
  h: number // 最高价
  l: number // 最低价
  c: number // 收盘价
  v: number // 成交量
}

/** 指标展示方式：overlay=主图叠加线 / pane=独立副图 / scalar=仅徽标数值 */
export type IndicatorKind = 'overlay' | 'pane' | 'scalar'

/** 指标序列数据点：time 为 Unix 秒（与 K 线一致），value 由数字字符串适配而来（该根无值为 null） */
export interface IndicatorSeriesPoint {
  time: number
  value: number | null
}

/** 单个指标的序列条目：label 为后端展示名（如 'EMA20（指数均线）'）；scalar 的 fields 可为空、当前值在 current */
export interface IndicatorSeriesEntry {
  label: string
  kind: IndicatorKind
  fields: Record<string, IndicatorSeriesPoint[]> // 字段名 → 序列（如 macd 的 dif/dea/hist）
  current?: number | null // scalar 指标当前值（如 oi 持仓量；缺失/无数据显示「无数据」）
}

/** 指标序列响应：GET /api/indicators/series?contract=&interval=&limit=&keys= */
export interface IndicatorSeriesResponse {
  contract: string
  interval: string
  series: Record<string, IndicatorSeriesEntry> // key（如 ema20）→ 序列条目
}

/** 可用指标定义：GET /api/indicator_config 的 available 数组项 */
export interface IndicatorAvailable {
  key: string // 指标 key（如 ema20 / macd / oi）
  label: string // 展示名（如 'EMA20（指数均线）'）
  kind: IndicatorKind
  fields: string[] // 序列字段名（overlay/pane 有意义；scalar 为空或单字段）
}

/** 指标配置：GET /api/indicator_config（shortlist=当前策略短名单 key 列表，保持后端顺序） */
export interface IndicatorConfig {
  shortlist: string[]
  available: IndicatorAvailable[]
}

/** 手动平仓结果：POST /api/positions/{contract}/close */
export interface ClosePositionResult {
  contract: string
  status: string // 平仓状态
  fill_price: number // 成交均价
  text: string // 结果描述文本
}

/** DELETE /api/orders/{contract}/{order_id} 的撤单结果。 */
export interface CancelOpenOrderResult {
  id: string
  contract: string
  status: string
  finish_as: string
  warning: string
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
/** 权益历史及其权威基准：GET /api/equity */
export interface EquitySeries {
  initialEquity: number
  baselineSource: string
  points: EquityPoint[]
}

export interface Note {
  time: string
  content: string
  round_id: string // 归属决策轮 ID（空串 = 无归属，如历史/手动记录）
}

/** Agent 笔记分页结果：GET /api/notes?offset=&limit=。 */
export type NotesPageResult = PageResult<Note>

/** 当前交易计划：GET /api/plan（全局唯一一份自由文本；content 空串 = 无计划）。 */
export interface TradePlan {
  content: string // 计划全文（Markdown）；空串表示当前无计划
  roundId: string // 最近一次写入计划的决策轮 ID（由 round_id 适配）
  updatedAt: string | null // 更新时间（ISO 字符串，由 updated_at(Unix秒) 适配）；无计划时 null
}

/** 当日统计：GET /api/daily_stats（风控同一口径：服务器时区自然日、按 mode 过滤、仅开仓单计数） */
export interface DailyStats {
  realized_pnl: number // 当日已实现盈亏合计（USDT，未扣费）
  orders_today: number // 当日开仓单数（平仓/减仓单不计）
  max_orders_per_day: number // 日下单上限（risk.max_orders_per_day 回显）
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

/** 官方交易所代码：用于开盘预设与自定义日期规则。 */
export type ResearchMarketCode = 'XTKS' | 'XLON' | 'XNYS'

/** 自定义日期规则：daily=每天，其余值表示对应交易所的交易日。 */
export type ResearchCalendarCode = 'daily' | ResearchMarketCode

/** 不可删除的市场开盘前研报预设。 */
export interface MarketOpenResearchSchedule {
  id: 'asia_open' | 'europe_open' | 'us_open'
  kind: 'market_open'
  market: ResearchMarketCode
  enabled: boolean
  lead_minutes: 30
}

/** 用户添加的 UTC+8 固定时间研报。 */
export interface FixedTimeResearchSchedule {
  id: string
  kind: 'fixed_time'
  enabled: boolean
  time: string
  calendar: ResearchCalendarCode
}

/** 自动研报调度项。 */
export type ResearchSchedule = MarketOpenResearchSchedule | FixedTimeResearchSchedule

/** 配置中心实际编辑的研报自动执行子集。 */
export interface ResearchScheduleConfig {
  enabled: boolean
  schedules: ResearchSchedule[]
}

/** 后端 research 完整配置；自动执行表单只提交其中两个热生效字段。 */
export interface ResearchConfig extends ResearchScheduleConfig {
  max_turns: number
  timeout_seconds: number
  jin10_mcp_url: string
  blockbeats_mcp_cmd: string
  fred_base_url: string
  polymarket_base_url: string
  gate_enabled: boolean
  gate_max_age_hours: number
}

/** 单个调度项的只读运行状态。 */
export interface ResearchScheduleStatusItem {
  id: string
  kind: ResearchSchedule['kind']
  enabled: boolean
  next_run_at: number | null
}

/** 官方交易日日历的只读运行状态。 */
export interface ResearchCalendarStatus {
  state: 'ok' | 'fallback' | 'error'
  last_refreshed_at: number | null
  errors: Record<string, string>
  warning: string
}

/** GET /api/research/schedule-status 的完整响应。 */
export interface ResearchScheduleStatus {
  enabled: boolean
  items: ResearchScheduleStatusItem[]
  calendar: ResearchCalendarStatus
}

/** LLM 凭证定义：llm.credentials 数组项；增改删只经 /api/credentials 专用端点。 */
export interface CredentialConfig {
  name: string // 凭证名（小写字母数字连字符，如 claude-main）
  provider: 'anthropic' | 'openai_compat' | 'openai_responses'
  model: string
  max_tokens: number
  openai_base_url: string // provider 为 openai_compat / openai_responses 时的接口地址（可空）
  thinking_effort: string // 思考程度：空=跟随模型默认 / off / on / low / medium / high / xhigh / max
  api_key_env: string // 该凭证 key 在服务器 .env 中的变量名（如 LLM_KEY_CLAUDE_MAIN）
}

/** 可编辑配置：GET/PUT /api/config（与 config.yaml 的可编辑子集对齐） */
export interface AppConfig {
  mode: string // 运行模式
  llm: {
    provider: string // anthropic / openai_compat / openai_responses（旧平铺字段；credentials 非空时由凭证接管）
    model: string
    max_tokens: number
    openai_base_url: string
    thinking_effort: string // 思考程度（旧平铺字段；凭证接管时以凭证为准）
    max_consecutive_failures: number
    credentials?: CredentialConfig[] // 多凭证列表；缺失 = 旧版单凭证（default）配置
  }
  agents?: {
    // 按 agent 分配凭证（缺失 = 旧配置，三个 Agent 均使用 default 凭证）
    trader?: { credential: string } // 决策 agent 使用的凭证名；部分旧配置可能缺失
    reviewer?: { credential: string } // 复盘 agent 使用的凭证名；部分旧配置可能缺失
    researcher?: { credential: string } // 研报 agent 使用的凭证名；旧配置可能缺失
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
  research?: ResearchConfig
}

/** 配置局部 PUT；research 允许只提交热生效的 enabled/schedules。 */
export type AppConfigPatch = Omit<Partial<AppConfig>, 'research'> & {
  research?: Partial<ResearchConfig>
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

/** 凭证配置状态：GET /api/secrets/status 的 credentials 数组项（永无明文） */
export interface CredentialStatus {
  name: string // 凭证名
  provider: 'anthropic' | 'openai_compat' | 'openai_responses'
  model: string
  api_key_env: string // 该凭证 key 在服务器 .env 中的变量名
  key_configured: boolean // 该凭证的 key 是否已配置
  used_by: string[] // 引用该凭证的 agent 名（trader=决策 / reviewer=复盘 / researcher=研报）
}

/** 密钥配置状态：GET /api/secrets/status（只返回布尔与凭证状态，永不返回明文） */
export interface SecretsStatus {
  gate_key: boolean // 交易所 API key 是否已配置
  llm_key: boolean // LLM key 是否已配置（任一凭证已配置即为 true）
  telegram: boolean // Telegram token 是否已配置
  credentials: CredentialStatus[] // 凭证列表（旧配置后端合成 default；防御性按可缺失处理）
}

/** LLM 密钥保存请求体：POST /api/secrets（空串 = 不改动该项，发送前剔除） */
export interface SetSecretsBody {
  anthropic_api_key?: string // Anthropic API Key（非空才发送，旧形式）
  openai_api_key?: string // OpenAI 兼容接口 Key（非空才发送，旧形式）
  credential?: string // 凭证名（新形式：与 api_key 成对，按凭证写 .env 对应键）
  api_key?: string // 凭证 key 明文（仅传输，后端写 .env 后不落响应）
}

/** LLM 密钥保存结果：POST /api/secrets 响应（永不含密钥明文） */
export interface SetSecretsResult {
  saved: boolean // 是否已写入服务器 .env
  llm_configured: boolean // 保存后 LLM 是否已配置可用
  error: string // 错误信息（如 provider 重建失败，空串 = 正常）
}

/** 新建 LLM 凭证请求体：POST /api/credentials 在一次请求中顺序保存定义与 key。 */
export interface CredentialCreateBody {
  name: string // 凭证名（小写字母数字连字符，创建后不可改）
  provider: 'anthropic' | 'openai_compat' | 'openai_responses'
  model: string
  max_tokens: number
  openai_base_url: string // provider 为 openai_compat / openai_responses 时的接口地址（可空）
  thinking_effort: string // 思考程度：空=跟随模型默认 / off / on / low / medium / high / xhigh / max
  api_key?: string // 凭证 key 明文（仅传输，后端写 .env 后不落响应；空串/缺省 = 不写 .env）
}

/** 更新 LLM 凭证请求体：PUT /api/credentials/{name}（路径参数即身份，故无 name；api_key_env 保持不变） */
export type CredentialUpdateBody = Omit<CredentialCreateBody, 'name'>

/** 凭证增/改/删统一响应：POST/PUT/DELETE /api/credentials（永不含密钥明文） */
export interface CredentialMutationResult {
  saved: boolean // 凭证定义是否已写入 config.yaml
  key_saved: boolean // 本次是否携带了非空 api_key 并已写入 .env
  llm_configured: boolean // 热重建后 LLM 是否已配置可用
  llm_error: string // provider 重建错误（空串 = 正常）
}

/** kill_switch 操作响应：POST /api/kill_switch */
export interface KillSwitchResult {
  kill_switch: boolean
}

/** 复盘报告摘要：GET /api/review/reports（列表项 reportMd 截断 200 字符，省流量） */
export interface ReviewReportSummary {
  id: number // 报告 ID
  periodStart: string // 统计区间起（ISO 字符串，由 period_start(Unix秒) 适配）
  periodEnd: string // 统计区间止（ISO 字符串，由 period_end(Unix秒) 适配）
  statsJson: string // 代码侧统计 JSON 原文（展示方自行解析，字段缺失时降级）
  reportMd: string // 复盘报告 markdown（列表截断 200 字符）
  strategyAction: 'none' | 'rewrite' // 策略动作：none=未调整 / rewrite=改写策略书
  newVersionId: number | null // rewrite 产出的策略版本 ID（none 时为 null）
  error: string // 非空 = 该次复盘失败（只落错误记录，不影响交易循环）
  roundId: string // 复盘审计轮 ID（空串 = 无关联，功能上线前的老报告）
  time: string // 创建时间（ISO 字符串，由 created_at(Unix秒) 适配）
}

/** 复盘报告详情：GET /api/review/reports/{id}（同摘要 10 键，reportMd 为全文） */
export type ReviewReport = ReviewReportSummary

/** 复盘报告分页：GET /api/review/reports（后端仅返回 items/total，无 offset/limit 回显） */
export interface ReviewReportsPage {
  items: ReviewReportSummary[]
  total: number
}

/** 手动触发复盘点火响应：POST /api/review/run 点火即返回（409=进行中 / 503=未配置 / 422=invalid_period，经 ApiError 抛出）；
 * 进度与结果经 WS 事件与 /api/review/live 轮询呈现（失败报告也落库入列），响应不含执行结果。 */
export interface RunReviewResult {
  started: boolean // 是否已点火启动复盘（成功恒为 true）
  periodStart?: number // 复盘统计区间起（Unix 秒，由 period_start 适配回显）
  periodEnd?: number // 复盘统计区间止（Unix 秒，由 period_end 适配回显）
  error?: string // 失败原因（空串/缺省 = 正常）
  errorCode?: string // 错误码（由 error_code 适配）
}

/** 逐标的结论摘要：列表接口不包含证据、风险、研判和市场快照。 */
export type ResearchTechnicalConfirmation = '确认' | '冲突' | '中性' | '不可用'

export interface ResearchAssetSummary {
  contract: string
  direction: string
  confidence: string
  horizon: string
  marketRegime: string
  technicalConfirmation: ResearchTechnicalConfirmation
  basisType: string
  dataStatus: string
}

/** 逐标的结论详情；marketContext(市场快照)只保存在后端，不进入 API。 */
export interface ResearchAssetDetail extends ResearchAssetSummary {
  evidence: string[]
  risks: string[]
  narrative: string
  verifyResult: string
  time: string
}

/** 研报摘要：报告头只含当前协议字段，成功项必须有逐标的摘要。 */
export interface ResearchReportSummary {
  id: number
  reportType: string
  schemaVersion: number
  summary: string
  crossMarketView: string
  globalRisks: string[]
  assetViews: ResearchAssetSummary[]
  error: string
  roundId: string
  time: string
}
/** 因果链节点：chain 已解析为有序数组（timeline_id 溯源事实层 timeline 条目，可缺省） */
export interface ChainNode {
  node: string // 节点内容（事件/数据/判断的描述文本）
  kind: string // 节点类型（中文自由文本：事件/推断/市场反应/标的结论；空串降级）
  timeline_id?: number // 溯源 timeline 条目 ID（有值时小字标注）
}

/** 因果链视图：研报详情内嵌（chain/evidence 已由后端解析为对象） */
export interface CausalLinkView {
  id: number // 因果链 ID
  reportId: number // 归属研报 ID（由 report_id 适配）
  chain: ChainNode[] // 有序节点链（待验证 1-6 节点 / 结论 2-6 节点）
  confidence: number // 链式置信度（0-1）
  evidence: string[] // 支撑证据列表
  status: string // 状态：pending(待验证) / verified(已确认) / failed(已否决) / superseded(已被替代)
  brokenAt: number | null // 断点节点下标（复盘标记；null = 未定位）
  topic: string // 事件主题（同主题链聚合成族；空串 = 旧数据无主题）
  supersedesId: number | null // 本链替代的旧链 ID（修正版有值；null = 非修正版）
  awaitVerification: boolean // 待验证声明：true=未闭合中间态（继续监控）/ false=结论链
  time: string // 创建时间（ISO 字符串，由 created_at(Unix秒) 适配）
}

/** 研报详情：报告头形状不变，逐标的结论展开并附带因果链。 */
export interface ResearchReportDetail extends Omit<ResearchReportSummary, 'assetViews'> {
  assetViews: ResearchAssetDetail[]
  causalLinks: CausalLinkView[]
}
/** 研报分页：GET /api/research/reports（后端仅返回 items/total，无 offset/limit 回显） */
export interface ResearchReportsPage {
  items: ResearchReportSummary[]
  total: number
}

/** 手动触发研报点火响应：POST /api/research/run 点火即返回（409=生成中 / 503=LLM 未配置 / 422=hours 越界，经 ApiError 抛出）；
 * 进度与结果经 WS 事件与 /api/research/live 轮询呈现（失败报告也落库入列），响应不含执行结果。 */
export interface RunResearchResult {
  started: boolean // 是否已点火启动研报（成功恒为 true）
  reportType?: string // 研报类型回显（由 report_type 适配）
  hours?: number // 覆盖窗口小时数回显
  error?: string // 失败原因（空串/缺省 = 正常）
  errorCode?: string // 错误码（由 error_code 适配）
}

/** 实时研报轮快照：GET /api/research/live 的 round 字段（形状与 AgentLiveRound 一致并透传 snake_case） */
export type ResearchLiveRound = AgentLiveRound

/** 实时研报状态：GET /api/research/live（形状同 /api/review/live；无研报轮时 round 为 null、tool_calls 为空） */
export interface ResearchLive {
  round: ResearchLiveRound | null // 当前（进行中）或上一研报轮；从未研报时为 null
  tool_calls: ToolCall[] // 本轮已执行的研报工具调用（进行中实时追加）
}

/** 实时决策轮可展示的 agent 种类：trader=交易 / review=复盘 / research=研报 */
export type LiveAgentKind = 'trader' | 'review' | 'research'

/**
 * 三端点（/api/agent/live、/api/review/live、/api/research/live）归一后的实时轮快照：
 * 丢弃端点私有字段（in_round / strategy_md5），进行中一律以 round.ended_at === null 判定；
 * 三端点统一在轮结束后保留终态轮（ended_at 非 null），仅当该 agent 从未运行时 round 为 null。
 */
export interface LiveSnapshot {
  round: AgentLiveRound | null // 当前轮（进行中）或上一轮（含刚结束的终态轮）；该 agent 从未运行时为 null
  tool_calls: ToolCall[] // 本轮已执行的工具调用（进行中实时追加）
}

/** 策略版本（列表项不含 content 全文）：GET /api/strategy/versions */
export interface StrategyVersion {
  id: number // 版本号（vN 的 N）
  md5: string // 策略书原文 md5（与决策轮 strategyMd5 关联）
  createdBy: string // 来源：human(人工) / review_agent(复盘) / rollback(回滚)
  reason: string // 变更理由
  reportId: number | null // 触发本版本的复盘报告 ID（人工版本为 null）
  time: string // 创建时间（ISO 字符串，由 created_at(Unix秒) 适配）
}

/** 策略版本详情：GET /api/strategy/versions/{id}（含 content 全文） */
export interface StrategyVersionDetail extends StrategyVersion {
  content: string // 策略书完整原文
}

/** 回滚结果：POST /api/strategy/rollback/{id}（回滚 = 写回历史内容 + 记 rollback 新版本） */
export interface RollbackResult {
  rolledBackTo: number // 回滚目标版本号
  version: number // 回滚产生的新版本号
}

/**
 * WS 推送消息：/ws → {type, data}
 * 当前契约：后端广播 hello / round_start(data={wake_source}) /
 * round(data={round_id, ok, wake_source}) / ticker（按合约节流，data={contract,last}） /
 * trades_updated(data={contracts, count}，成交落库成功，本批合约去重+笔数) /
 * review_round_start(data={round_id}) / review_round(data={round_id, ok})（复盘轮开始/结束）/
 * research_round_start(data={round_id}) / research_round(data={round_id, ok})（研报轮开始/结束）；
 * 注意 round 的 data 并非完整 RoundSummary（无 started_at/summary），trades_updated
 * 也不携带成交明细，两者均只作失效信号——消费方应据事件重拉 REST，勿把 payload 当数据直接渲染；
 * 后端当前不生产 trade/position；类型仅供 mock 使用，真实消费前必须按后端实际事件接线。
 */
export type WsMessage =
  | { type: 'round_start'; data: { wake_source: string } }
  | { type: 'round'; data: { round_id: string; ok: boolean; wake_source: string } }
  | { type: 'trade'; data: Trade }
  | { type: 'position'; data: Position }
  | { type: 'ticker'; data: { contract: string; last: number } }
  | { type: 'trades_updated'; data: { contracts: string[]; count: number } }
  | { type: 'plan_updated' }
  | { type: 'strategy_updated' }
  | { type: 'indicator_config_updated' } // 指标短名单变更：payload 无约定，仅作失效信号重拉 REST
  | { type: 'review_round_start'; data: { round_id: string } } // 复盘轮开始：进度条进入进行中态，实时数据走 /api/review/live
  | { type: 'review_round'; data: { round_id: string; ok: boolean } } // 复盘轮结束：仅作失效信号，消费方重拉报告列表
  | { type: 'research_round_start'; data: { round_id: string } } // 研报轮开始：进度条进入进行中态，实时数据走 /api/research/live
  | { type: 'research_round'; data: { round_id: string; ok: boolean } } // 研报轮结束：仅作失效信号，消费方重拉研报列表

/** REST 客户端统一接口（http.ts 真实实现 / mock.ts 假数据实现） */
export interface ApiClient {
  getStatus(): Promise<StatusInfo>
  getAccount(): Promise<AccountInfo>
  getPositions(): Promise<Position[]>
  getPortfolio(): Promise<PortfolioSnapshot>
  /** 读取交易所或模拟撮合引擎当前仍为 open 的订单。 */
  getOpenOrders(): Promise<OpenOrder[]>
  /** 读取 LLM 设置的未触发价格唤醒。 */
  getAlerts(): Promise<PriceAlert[]>
  getRounds(offset: number, limit: number): Promise<RoundsPageResult>
  getRound(roundId: string): Promise<RoundDetail>
  getAgentLive(): Promise<AgentLiveState>
  getTrades(offset: number, limit: number, contract?: string): Promise<TradesPageResult>
  getCandles(contract: string, interval: string, limit?: number): Promise<Candle[]>
  /** 当前策略指标短名单与可用指标全集（kind：overlay=主图叠加 / pane=副图 / scalar=徽标数值）。 */
  getIndicatorConfig(): Promise<IndicatorConfig>
  /** 批量拉取指标序列；前端始终显式传 keys（短名单里的非 scalar 项），scalar 当前值随响应一并返回。 */
  getIndicatorSeries(
    contract: string,
    interval: string,
    keys: string[],
    limit?: number,
  ): Promise<IndicatorSeriesResponse>
  closePosition(contract: string): Promise<ClosePositionResult>
  /** 撤销指定合约和订单 ID；已终态订单由调用方刷新列表。 */
  cancelOpenOrder(contract: string, orderId: string): Promise<CancelOpenOrderResult>
  resetPaperEquity(equity: number): Promise<PaperResetResult>
  startAgent(): Promise<AgentStateResult>
  stopAgent(): Promise<AgentStateResult>
  getEquity(): Promise<EquitySeries>
  getNotes(offset?: number, limit?: number): Promise<NotesPageResult>
  /** 当前交易计划（全局唯一一份）；无计划时 content 为空串、updatedAt 为 null。 */
  getPlan(): Promise<TradePlan>
  getDailyStats(): Promise<DailyStats>
  getConfig(): Promise<AppConfig>
  putConfig(config: AppConfigPatch): Promise<PutConfigResult>
  getStrategy(): Promise<string>
  putStrategy(content: string): Promise<string>
  getWatchlist(): Promise<Watchlist>
  putWatchlist(list: Watchlist): Promise<Watchlist>
  getSecretsStatus(): Promise<SecretsStatus>
  setSecrets(body: SetSecretsBody): Promise<SetSecretsResult>
  /** 新建 LLM 凭证；定义先落 config.yaml，key 再写 .env，重名 422 经 ApiError 抛出。 */
  createCredential(body: CredentialCreateBody): Promise<CredentialMutationResult>
  /** 更新指定凭证（api_key_env 不变）；未知名 404 经 ApiError 抛出。 */
  updateCredential(name: string, body: CredentialUpdateBody): Promise<CredentialMutationResult>
  /** 删除指定凭证；未知名 404、被 agents 引用中 422 经 ApiError 抛出。 */
  deleteCredential(name: string): Promise<CredentialMutationResult>
  setKillSwitch(enabled: boolean): Promise<KillSwitchResult>
  /** 复盘报告分页列表（最新在前）；reportMd 截断 200 字符。 */
  getReviewReports(offset: number, limit: number): Promise<ReviewReportsPage>
  /** 复盘报告详情：reportMd 全文；404 经 ApiError 抛出。 */
  getReviewReport(id: number): Promise<ReviewReport>
  /** 手动触发复盘（区间为最近 interval_days 天）；409=进行中、503=LLM 未配置/未接线（ApiError.detail 可读）。 */
  runReview(): Promise<RunReviewResult>
  /** 实时复盘状态：round/tool_calls 形状与 getAgentLive 一致（进行中 ended_at 为 null），无复盘轮时 round 为 null。 */
  getReviewLive(): Promise<ReviewLive>
  /** 研报分页列表（最新在前）；narrative 截断 200 字符。 */
  getResearchReports(offset: number, limit: number): Promise<ResearchReportsPage>
  /** 研报详情：narrative 全文 + evidence/risks/raw 已解析 + 因果链；404 经 ApiError 抛出。 */
  getResearchReport(id: number): Promise<ResearchReportDetail>
  /** 手动触发研报；409=生成中、503=LLM 未配置、422=hours 越界（ApiError.detail 可读）。 */
  runResearch(reportType?: string, hours?: number): Promise<RunResearchResult>
  /** 实时研报状态：形状同 getReviewLive（进行中 ended_at 为 null），无研报轮时 round 为 null。 */
  getResearchLive(): Promise<ResearchLive>
  /** 自动研报的下次执行时间与官方日历状态。 */
  getResearchScheduleStatus(): Promise<ResearchScheduleStatus>
  /** 按 agent 取实时轮快照（三端点归一为 LiveSnapshot；进行中以 round.ended_at === null 判定）。 */
  getLiveFor(agent: LiveAgentKind): Promise<LiveSnapshot>
  /** 策略版本列表（最新在前，不含 content 全文）。 */
  getStrategyVersions(): Promise<StrategyVersion[]>
  /** 策略版本详情（含 content 全文）；404 经 ApiError 抛出。 */
  getStrategyVersion(id: number): Promise<StrategyVersionDetail>
  /** 两版本策略书 unified diff（纯文本）。 */
  getStrategyDiff(fromId: number, toId: number): Promise<string>
  /** 回滚到指定策略版本（生成 rollback 新版本）；404 经 ApiError 抛出。 */
  rollbackStrategy(id: number): Promise<RollbackResult>
}
