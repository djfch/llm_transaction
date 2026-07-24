/**
 * Mock 实现：与 ApiClient 同接口的假数据，供后端未就绪时前端独立开发预览。
 * 数据为内存态，PUT/POST 会修改内存副本（刷新页面后复原）。
 */
import { ApiError } from './http'
import type {
  AgentLiveState,
  ApiClient,
  AppConfig,
  Candle,
  EquityPoint,
  Note,
  OpenOrder,
  Position,
  RoundDetail,
  RoundSummary,
  ToolCall,
  Trade,
} from './types'

/** 测试环境零延迟，开发环境模拟网络延迟 */
const LATENCY = import.meta.env.MODE === 'test' ? 0 : 120

function reply<T>(value: T): Promise<T> {
  return new Promise((resolve) => setTimeout(() => resolve(value), LATENCY))
}

/** 生成过去 n 个小时的时间序列（升序，ISO 字符串） */
function hoursAgoSeries(n: number): Date[] {
  const now = Date.now()
  return Array.from({ length: n }, (_, i) => new Date(now - (n - 1 - i) * 3600_000))
}

// ---------- 内存态假数据 ----------

let killSwitch = false
let agentRunning = true // 交易 Agent 运行状态（内存态）
let paperEquity = 10_842.36 // paper 模式权益（resetPaperEquity 可改）
let llmConfigured = true // LLM API Key 配置状态（setSecrets 可改）
const bootTime = Date.now() - 26 * 3600_000 // 假设已运行 26 小时

const positions: Position[] = [
  {
    contract: 'BTC_USDT',
    size: 12,
    entry_price: 118_320,
    mark_price: 119_650,
    leverage: 3,
    margin: 47.86, // 12 张 × 0.0001 × 119650 / 3（quanto 面值推算）
    unrealised_pnl: 159.6,
    liq_price: 82_400,
    stop_loss_price: 116_800,
    take_profit_price: 122_400,
  },
  {
    contract: 'ETH_USDT',
    size: -30,
    entry_price: 3_420,
    mark_price: 3_388,
    leverage: 2,
    margin: 50.82, // 30 张 × 0.001 × 3388 / 2（quanto 面值推算）
    unrealised_pnl: 96,
    liq_price: 5_120,
    stop_loss_price: 3_510,
    take_profit_price: null,
  },
]

// mock 模式的可撤销挂单，用于覆盖真实接口不可用时的完整交互。
const openOrders: OpenOrder[] = [
  {
    id: 'mock-open-1',
    contract: 'ETH_USDT',
    size: 79,
    left: 79,
    price: 1900,
    tif: 'gtc',
    reduce_only: false,
    status: 'open',
    stop_loss_price: 1850,
    take_profit_price: 2050,
  },
]

const config: AppConfig = {
  mode: 'paper',
  llm: {
    provider: 'anthropic',
    model: 'claude-sonnet-4-5',
    max_tokens: 4096,
    openai_base_url: '',
    max_consecutive_failures: 3,
  },
  risk: {
    max_position_pct: 0.3,
    max_total_position_pct: 0.8,
    max_leverage: 5,
    daily_loss_limit: 0.1,
    max_orders_per_day: 20,
    max_deviation: 0.02,
    kill_switch: false,
  },
  scheduler: { default_wake_minutes: 60, min_wake_minutes: 5, max_wake_minutes: 720 },
  notify: { telegram_enabled: false },
}

let strategy = `# 系统提示词（system_prompt.md）\n\n你是 Gate.io USDT 永续合约的自主交易 Agent。\n\n## 原则\n\n1. 保本优先，单笔风险不超过权益的 2%。\n2. 只交易白名单合约，遵守风控参数。\n3. 每次决策说明理由，并给出下次唤醒时间。\n`

const watchlist = { settle: 'usdt', contracts: ['BTC_USDT', 'ETH_USDT'] }

const WAKE_SOURCES = ['定时唤醒', '价格触发', '启动']

/** 生成 37 轮决策摘要（倒序：最新在前），用于演示分页。 */
function buildRounds(): RoundSummary[] {
  return Array.from({ length: 37 }, (_, i) => {
    const seq = 37 - i
    const started = new Date(Date.now() - i * 3600_000)
    const pnl = Math.round((seq * 12.5 - 180 + (seq % 5) * 30) * 100) / 100
    return {
      round_id: `round-${String(seq).padStart(4, '0')}`,
      started_at: started.toISOString(),
      wake_source: WAKE_SOURCES[seq % WAKE_SOURCES.length],
      summary:
        seq % 4 === 0
          ? 'BTC 突破阻力位，尝试加多被风控拒绝；维持现有持仓。'
          : '波动率不足，维持现有持仓，继续观察。',
      pnl_after: pnl,
    }
  })
}
const rounds = buildRounds()

const TRADE_SOURCES = ['llm_open', 'llm_close', 'user_close', 'liquidation', 'tpsl_close', '']

/** llm 开/平仓成交归属的决策轮 ID：按成交时间映射到每小时一轮的 rounds 下标（其余来源无归属，空串） */
function llmTradeRoundId(i: number, source: string): string {
  if (source !== 'llm_open' && source !== 'llm_close') return ''
  return rounds[Math.floor(((42 - i) * 2700_000) / 3_600_000)]?.round_id ?? ''
}

const trades: Trade[] = Array.from({ length: 42 }, (_, i) => {
  const contract = i % 3 === 0 ? 'ETH_USDT' : 'BTC_USDT'
  const long = i % 2 === 0
  const price = contract === 'BTC_USDT' ? 115_000 + i * 96 : 3_350 + i * 4
  const source = TRADE_SOURCES[i % TRADE_SOURCES.length]
  return {
    id: i + 1,
    round_id: llmTradeRoundId(i, source),
    time: new Date(Date.now() - (42 - i) * 2700_000).toISOString(),
    contract,
    size: long ? 4 + (i % 5) : -(4 + (i % 5)),
    price: Math.round(price * 100) / 100,
    fee: Math.round(price * 0.0005 * 100) / 100,
    pnl: Math.round(((i % 7) * 18 - 54) * 100) / 100,
    source,
  }
})

// 归属成交的轮：摘要改写为与成交一致，保证「成交记录 → 决策轮卡片」链接演示叙事自洽
for (const t of trades) {
  const meta = rounds.find((r) => r.round_id === t.round_id)
  if (t.round_id && meta) {
    const action = t.source === 'llm_open' ? (t.size > 0 ? '开多' : '开空') : '平仓'
    meta.summary = `${t.contract} ${action} ${Math.abs(t.size)} 张 @ ${t.price}。`
  }
}

const equity: EquityPoint[] = hoursAgoSeries(200).map((t, i) => ({
  time: t.toISOString(),
  equity: Math.round((10_000 + i * 4.2 + Math.sin(i / 6) * 120) * 100) / 100,
}))

function buildPortfolio() {
  const drift = import.meta.env.MODE === 'test' ? 0 : Math.round(Math.sin(Date.now() / 3000) * 20 * 100) / 100
  const livePositions = positions.map((position, index) =>
    index === 0
      ? {
          ...position,
          mark_price: position.mark_price + drift,
          unrealised_pnl: position.unrealised_pnl + drift,
        }
      : { ...position },
  )
  return {
    asOf: new Date().toISOString(),
    account: {
      equity: paperEquity + drift,
      available: Math.max(0, Math.round((paperEquity - 3_527.16) * 100) / 100),
      unrealised_pnl: livePositions.reduce((sum, position) => sum + position.unrealised_pnl, 0),
    },
    positions: livePositions,
  }
}

// round_id：部分归属 mock 决策轮（时间线卡片嵌引文演示），部分空串（无归属）
const notes: Note[] = [
  { time: new Date(Date.now() - 3600_000).toISOString(), content: 'BTC 4h 级别仍处上升通道，回调即加多机会。', round_id: rounds[0].round_id },
  { time: new Date(Date.now() - 7200_000).toISOString(), content: '资金费率偏高，注意多头持仓成本。', round_id: '' },
  { time: new Date(Date.now() - 10_800_000).toISOString(), content: 'ETH/BTC 汇率走弱，空 ETH 对冲仓位继续持有。', round_id: rounds[2].round_id },
  ...Array.from({ length: 9 }, (_, i) => ({
    time: new Date(Date.now() - (i + 4) * 3600_000).toISOString(),
    content: `第 ${i + 4} 条模拟策略笔记：等待价格与成交量共同确认。`,
    round_id: i % 2 === 0 ? rounds[i + 4]?.round_id ?? '' : '',
  })),
]

/** K 线周期间隔（秒），用于 mock 时间轴 */
const INTERVAL_SECONDS: Record<string, number> = {
  '1m': 60,
  '5m': 300,
  '15m': 900,
  '1h': 3600,
  '4h': 14400,
  '1d': 86400,
}

/** 确定性伪随机 K 线：以合约为种子的简单 LCG，固定生成 limit 根（同一合约多次请求结果一致） */
function buildCandles(contract: string, interval: string, limit: number): Candle[] {
  let seed = 42
  for (const ch of contract + interval) seed = (seed * 31 + ch.charCodeAt(0)) % 2_147_483_647
  const rand = () => {
    seed = (seed * 1_664_525 + 1_013_904_223) % 2_147_483_647
    return seed / 2_147_483_647
  }
  const step = INTERVAL_SECONDS[interval] ?? 3600
  const base = contract.startsWith('BTC') ? 115_000 : contract.startsWith('ETH') ? 3_350 : 100
  const end = Math.floor(Date.now() / 1000 / step) * step
  let prev = base
  return Array.from({ length: limit }, (_, i) => {
    const o = prev
    const c = o * (1 + (rand() - 0.5) * 0.02)
    const h = Math.max(o, c) * (1 + rand() * 0.005)
    const l = Math.min(o, c) * (1 - rand() * 0.005)
    prev = c
    const r = (n: number) => Math.round(n * 100) / 100
    return { t: end - (limit - 1 - i) * step, o: r(o), h: r(h), l: r(l), c: r(c), v: Math.round(rand() * 500) }
  })
}

// ---------- 接口实现 ----------

export const mockApi: ApiClient = {
  getStatus: () =>
    reply({
      mode: config.mode,
      uptime_seconds: Math.floor((Date.now() - bootTime) / 1000),
      kill_switch: killSwitch,
      llm_provider: config.llm.provider,
      llm_model: config.llm.model,
      llm_configured: llmConfigured,
      agent_running: agentRunning,
    }),
  getAccount: () =>
    // available 由 paperEquity 派生，避免设置金额后账户概览自相矛盾
    reply({ equity: paperEquity, available: Math.max(0, Math.round((paperEquity - 3_527.16) * 100) / 100), unrealised_pnl: 255.6 }),
  getPositions: () => reply(positions.map((p) => ({ ...p }))),
  getPortfolio: () => reply(buildPortfolio()),
  getOpenOrders: () => reply(openOrders.map((order) => ({ ...order }))),
  getRounds: (offset, limit) =>
    reply({ items: rounds.slice(offset, offset + limit), total: rounds.length, offset, limit }),
  getRound: (roundId) => {
    const meta = rounds.find((r) => r.round_id === roundId)
    if (!meta) return Promise.reject(new Error(`决策轮不存在: ${roundId}`))
    return reply(buildRoundDetail(meta))
  },
  // 样例：一条已完成决策轮（3 次工具调用），in_round=false 对应"上轮决策"展示
  getAgentLive: () => reply(buildAgentLive()),
  getTrades: (offset, limit, contract) => {
    const list = [...trades].reverse().filter((t) => !contract || t.contract === contract)
    return reply({ items: list.slice(offset, offset + limit), total: list.length, offset, limit })
  },
  getCandles: (contract, interval, limit = 200) => reply(buildCandles(contract, interval, limit)),
  closePosition: (contract) => {
    const idx = positions.findIndex((p) => p.contract === contract)
    if (idx < 0) return Promise.reject(new ApiError(404, `无持仓: ${contract}`))
    const [closed] = positions.splice(idx, 1)
    return reply({
      contract,
      status: 'closed',
      fill_price: closed.mark_price,
      text: `已按标记价 ${closed.mark_price} 市价平仓`,
    })
  },
  // 模拟撤单会从内存订单簿移除目标卡片，与真实接口的成功结果保持一致。
  cancelOpenOrder: (contract, orderId) => {
    const index = openOrders.findIndex((order) => order.contract === contract && order.id === orderId)
    if (index < 0) return Promise.reject(new ApiError(404, '挂单不存在'))
    const [cancelled] = openOrders.splice(index, 1)
    return reply({
      id: cancelled.id,
      contract: cancelled.contract,
      status: 'finished',
      finish_as: 'cancelled',
      warning: '',
    })
  },

  resetPaperEquity: (equity) => {
    if (config.mode !== 'paper') return Promise.reject(new ApiError(409, '当前非 paper 模式，无法重置权益'))
    paperEquity = equity
    return reply({ equity })
  },
  startAgent: () => {
    agentRunning = true
    return reply({ agent_running: agentRunning })
  },
  stopAgent: () => {
    agentRunning = false
    return reply({ agent_running: agentRunning })
  },
  getEquity: () =>
    reply({ initialEquity: 10_000, baselineSource: 'paper_config', points: equity }),
  getNotes: (offset = 0, limit = 20) =>
    reply({ items: notes.slice(offset, offset + limit), total: notes.length, offset, limit }),
  // 固定值：与 mock 成交叙事自洽（当日若干笔已实现合计），上限随风控配置联动
  getDailyStats: () =>
    reply({ realized_pnl: 41.37, orders_today: 7, max_orders_per_day: config.risk.max_orders_per_day }),
  getConfig: () => reply(structuredClone(config)),
  putConfig: (next) => {
    Object.assign(config, next)
    // 与后端契约对齐：{saved, needs_restart} + llm 热键（恒按变更处理）两键
    return reply({ saved: true, needs_restart: [], llm_configured: llmConfigured, llm_error: '' })
  },
  getStrategy: () => reply(strategy),
  putStrategy: (content) => {
    strategy = content
    return reply(strategy)
  },
  getWatchlist: () => reply(structuredClone(watchlist)),
  putWatchlist: (list) => {
    watchlist.contracts = list.contracts
    watchlist.settle = list.settle
    return reply(structuredClone(watchlist))
  },
  getSecretsStatus: () => reply({ gate_key: true, llm_key: llmConfigured, telegram: false }),
  setSecrets: (body) => {
    // 契约：空串/缺省 = 不改动；任一 key 非空则视为已配置（mock 无法模拟失败，error 恒空）
    const anthropic = body.anthropic_api_key ?? ''
    const openai = body.openai_api_key ?? ''
    if (anthropic !== '' || openai !== '') llmConfigured = true
    return reply({ saved: true, llm_configured: llmConfigured, error: '' })
  },
  setKillSwitch: (enabled) => {
    killSwitch = enabled
    return reply({ kill_switch: killSwitch })
  },
}

/** prompt 快照（各叙事共用，与 buildAgentLive 的上下文口径一致） */
function promptSnapshot(meta: RoundSummary): string {
  return [
    '# System Prompt（md5: 9f2c…a1）',
    '',
    strategy.trim(),
    '',
    '# 上下文',
    `唤醒来源: ${meta.wake_source}`,
    '账户权益: 10842.36 USDT，可用: 7315.20 USDT',
    '持仓: BTC_USDT +12 张（浮盈 +159.60），ETH_USDT -30 张（浮盈 +96.00）',
    'BTC_USDT 1h 近 20 根 K 线摘要: 震荡上行，收于均线上方。',
  ].join('\n')
}

/** Anthropic 原生响应单行 JSON：一个 assistant 回合（text + 若干 tool_use），多回合按 \n 连接成 llm_raw */
function anthropicTurn(
  text: string,
  toolUses: Array<{ name: string; input: Record<string, unknown> }> = [],
): string {
  const content: Array<Record<string, unknown>> = [{ type: 'text', text }]
  toolUses.forEach((tu, k) =>
    content.push({ type: 'tool_use', id: `toolu_mock_${k + 1}`, name: tu.name, input: tu.input }),
  )
  return JSON.stringify({ role: 'assistant', content })
}

/** 审计工具调用简写：风控默认空串（未入风控），duration_ms 按 seq 递增 */
function mockCall(
  seq: number,
  tool: string,
  args: Record<string, unknown>,
  result: string,
  verdict = '',
  reason = '',
): ToolCall {
  return { seq, tool, args, risk_verdict: verdict, risk_reason: reason, result, duration_ms: 3 + seq * 7 }
}

/** 有归属成交的轮：分析 → 下单(allow) → 成交结论；llm_raw 的 tool_use 与审计链逐条对应 */
function fillNarrative(fill: Trade): Pick<RoundDetail, 'llm_raw' | 'tool_calls'> {
  const action = fill.source === 'llm_open' ? (fill.size > 0 ? '开多' : '开空') : '平仓'
  const qty = Math.abs(fill.size)
  const klineArgs = { contract: fill.contract, interval: '1h', limit: 20 }
  const orderArgs = { contract: fill.contract, size: fill.size, price: '0', tif: 'ioc', stop_loss_price: fill.size > 0 ? fill.price * 0.98 : fill.price * 1.02 }
  const llm_raw = [
    anthropicTurn(`账户信息已注入上下文，检查 ${fill.contract} 走势。`, [{ name: 'get_market_data', input: klineArgs }]),
    anthropicTurn(`${fill.contract} 信号符合策略，${action} ${qty} 张。`, [
      { name: 'place_order', input: orderArgs },
    ]),
    anthropicTurn(`已${action} ${qty} 张 ${fill.contract}（成交价 ${fill.price}），30 分钟后复查。`),
  ].join('\n')
  const toolCalls: ToolCall[] = [
    mockCall(1, 'get_market_data', klineArgs, '返回 20 根 K 线'),
    mockCall(2, 'place_order', orderArgs, `已成交 ${fill.size} 张 @ ${fill.price}（成交ID ${fill.id}）`, 'allow'),
  ]
  return { llm_raw, tool_calls: toolCalls }
}

/** 突破语境但无成交的轮：加仓被风控拒绝(deny)，与「无成交」自洽 */
function denyNarrative(): Pick<RoundDetail, 'llm_raw' | 'tool_calls'> {
  const klineArgs = { contract: 'BTC_USDT', interval: '1h', limit: 20 }
  const orderArgs = { contract: 'BTC_USDT', size: 20, price: '0', tif: 'ioc', stop_loss_price: 112_000 }
  const noteArgs = { content: '突破有效性待确认，下次 30 分钟后唤醒。' }
  const reason = '下单后单仓名义价值占权益 36% > max_position_pct(单仓上限) 30%'
  const llm_raw = [
    anthropicTurn('账户信息已注入上下文，确认 BTC 突破后的 K 线形态。', [{ name: 'get_market_data', input: klineArgs }]),
    anthropicTurn('量能配合，尝试加仓 20 张 BTC。', [{ name: 'place_order', input: orderArgs }]),
    anthropicTurn('加仓被风控拒绝，维持现有持仓，记录观察结论。', [{ name: 'set_note', input: noteArgs }]),
  ].join('\n')
  const toolCalls: ToolCall[] = [
    mockCall(1, 'get_market_data', klineArgs, '返回 20 根 K 线'),
    mockCall(2, 'place_order', orderArgs, '风控拒绝，未下单', 'deny', reason),
    mockCall(3, 'set_note', noteArgs, '已保存笔记'),
  ]
  return { llm_raw, tool_calls: toolCalls }
}

/** 观望轮：账户已在上下文中，无交易动作，只记笔记 */
function idleNarrative(): Pick<RoundDetail, 'llm_raw' | 'tool_calls'> {
  const noteArgs = { content: '波动率不足，维持现有持仓，继续观察。' }
  const llm_raw = [
    anthropicTurn('账户信息已注入，波动率不足，记录观察结论。', [{ name: 'set_note', input: noteArgs }]),
    anthropicTurn('本轮无交易动作，60 分钟后定时唤醒。'),
  ].join('\n')
  const toolCalls: ToolCall[] = [
    mockCall(1, 'set_note', noteArgs, '已保存笔记'),
  ]
  return { llm_raw, tool_calls: toolCalls }
}

/** 构造一轮审计详情：按「归属成交 / 突破语境 / 观望」选择自洽叙事（llm_raw 为 Anthropic 原生格式） */
function buildRoundDetail(meta: RoundSummary): RoundDetail {
  const fill = trades.find((t) => t.round_id === meta.round_id)
  const body = fill ? fillNarrative(fill) : meta.summary.includes('突破') ? denyNarrative() : idleNarrative()
  return { round_id: meta.round_id, prompt_snapshot: promptSnapshot(meta), ...body }
}

/** 构造实时决策样例：复用最新一轮摘要，生成一条已完成决策轮。 */
function buildAgentLive(): AgentLiveState {
  const meta = rounds[0]
  const startedAt = Math.floor(new Date(meta.started_at).getTime() / 1000)
  return {
    in_round: false,
    round: {
      round_id: meta.round_id,
      wake_source: meta.wake_source,
      prompt_md5: '9f2c1a3b7e5d40f2a1b3c5d7e9f0a1b3',
      prompt_snapshot: [
        '# System Prompt（md5: 9f2c…a1）',
        '',
        strategy.trim(),
      ].join('\n'),
      context_snapshot: [
        `唤醒来源: ${meta.wake_source}`,
        '账户权益: 10842.36 USDT，可用: 7315.20 USDT',
        '持仓: BTC_USDT +12 张（浮盈 +159.60），ETH_USDT -30 张（浮盈 +96.00）',
        'BTC_USDT 1h 近 20 根 K 线摘要: 震荡上行，收于均线上方。',
      ].join('\n'),
      llm_raw: JSON.stringify(
        {
          thoughts: '波动率不足，维持现有持仓，记录观察结论后 30 分钟后再唤醒。',
          tool_calls: [
            { tool: 'write_note', args: { content: '突破有效性待确认，继续观察。' } },
            { tool: 'set_next_wakeup', args: { minutes: 30 } },
          ],
        },
        null,
        2,
      ),
      started_at: startedAt,
      ended_at: startedAt + 42,
      error: '',
    },
    tool_calls: [
      {
        seq: 1,
        tool: 'write_note',
        args: { content: '突破有效性待确认，继续观察。' },
        risk_verdict: '',
        risk_reason: '',
        result: { text: '已保存笔记' },
        duration_ms: 5,
      },
      {
        seq: 2,
        tool: 'set_next_wakeup',
        args: { minutes: 30 },
        risk_verdict: '',
        risk_reason: '',
        result: { text: '已设置 30 分钟后唤醒' },
        duration_ms: 2,
      },
    ],
  }
}
