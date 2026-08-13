/**
 * Mock 实现：与 ApiClient 同接口的假数据，供后端未就绪时前端独立开发预览。
 * 数据为内存态，PUT/POST 会修改内存副本（刷新页面后复原）。
 */
import { ApiError } from './http'
import { createIndicatorMock } from './mockIndicators'
import { createResearchMock } from './mockResearch'
import { createReviewMock } from './mockReview'
import type {
  AgentLiveState,
  ApiClient,
  AppConfig,
  Candle,
  CredentialStatus,
  EquityPoint,
  LiveSnapshot,
  Note,
  OpenOrder,
  PriceAlert,
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

// LLM 设置的未触发价格唤醒（与 mock 持仓叙事自洽：BTC 等上破、ETH 等下破）
const priceAlerts: PriceAlert[] = [
  { id: 1, contract: 'BTC_USDT', direction: 'above', price: 122_000, time: new Date(Date.now() - 3600_000).toISOString() },
  { id: 2, contract: 'ETH_USDT', direction: 'below', price: 3_300, time: new Date(Date.now() - 7200_000).toISOString() },
]

const config: AppConfig = {
  mode: 'paper',
  llm: {
    provider: 'anthropic',
    model: 'claude-sonnet-4-5',
    max_tokens: 4096,
    openai_base_url: '',
    thinking_effort: '',
    max_consecutive_failures: 3,
    // 多凭证夹具：anthropic 供决策/研报，openai_compat 供复盘
    credentials: [
      {
        name: 'claude-main',
        provider: 'anthropic',
        model: 'claude-sonnet-4-5',
        max_tokens: 4096,
        openai_base_url: '',
        thinking_effort: '',
        api_key_env: 'LLM_KEY_CLAUDE_MAIN',
      },
      {
        name: 'deepseek-backup',
        provider: 'openai_compat',
        model: 'deepseek-chat',
        max_tokens: 4096,
        openai_base_url: 'https://api.deepseek.com/v1',
        thinking_effort: '',
        api_key_env: 'LLM_KEY_DEEPSEEK_BACKUP',
      },
    ],
  },
  agents: {
    trader: { credential: 'claude-main' },
    reviewer: { credential: 'deepseek-backup' },
    researcher: { credential: 'claude-main' },
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

// 凭证 key 配置状态（与 config.llm.credentials 对应；setSecrets 可翻转 key_configured）
let credentials: CredentialStatus[] = [
  {
    name: 'claude-main',
    provider: 'anthropic',
    model: 'claude-sonnet-4-5',
    api_key_env: 'LLM_KEY_CLAUDE_MAIN',
    key_configured: true,
    used_by: ['trader', 'researcher'],
  },
  {
    name: 'deepseek-backup',
    provider: 'openai_compat',
    model: 'deepseek-chat',
    api_key_env: 'LLM_KEY_DEEPSEEK_BACKUP',
    key_configured: false,
    used_by: ['reviewer'],
  },
]

let strategy = `# 系统提示词（system_prompt.md）\n\n你是 Gate.io USDT 永续合约的自主交易 Agent。\n\n## 原则\n\n1. 保本优先，单笔风险不超过权益的 2%。\n2. 只交易白名单合约，遵守风控参数。\n3. 每次决策说明理由，并给出下次唤醒时间。\n`

// 复盘/策略版本 mock 域（报告、版本表与 7 个方法实现）拆分到 mockReview.ts；此处装配：
// 注入 reply 与 strategy 读写引用，回滚/人工保存经回调同步本文件的 strategy。
const reviewMock = createReviewMock(reply, {
  get: () => strategy,
  set: (content) => {
    strategy = content
  },
})

/** 版本表 md5 快照（最新在前）：决策轮 strategyMd5 关联演示用 */
const VERSION_MD5S = reviewMock.versionMd5s()

// 研报 mock 域（研报列表 + 因果链演示数据与 4 个方法实现）拆分到 mockResearch.ts；此处装配。
const researchMock = createResearchMock(reply)

// 指标 mock 域（短名单配置 + 由 buildCandles 确定性计算的序列）拆分到 mockIndicators.ts；
// buildCandles 为函数声明会提升，此处引用安全。
const indicatorMock = createIndicatorMock(reply, buildCandles)

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
      // 按序号轮转关联三个策略版本（演示版本标签 join）；每 5 轮一条空串（历史数据无关联，显示「—」）
      strategyMd5: seq % 5 === 0 ? '' : (VERSION_MD5S[seq % VERSION_MD5S.length] ?? ''),
      pnl_after: pnl,
      // 每 6 轮一条归属笔记引文（跨页分布，演示任意页都能显示笔记）
      note:
        seq % 6 === 1
          ? { content: `第 ${seq} 轮笔记：缩量观望，等待 4h 方向确认。`, time: started.toISOString() }
          : null,
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
  getStatus: () => {
    const credentialName = config.agents?.trader?.credential ?? 'default'
    const credential = config.llm.credentials?.find((item) => item.name === credentialName)
    return reply({
      mode: config.mode,
      uptime_seconds: Math.floor((Date.now() - bootTime) / 1000),
      kill_switch: killSwitch,
      llm_credential_name: credential?.name ?? 'default',
      llm_provider: credential?.provider ?? config.llm.provider,
      llm_model: credential?.model ?? config.llm.model,
      llm_thinking_effort: credential?.thinking_effort ?? config.llm.thinking_effort ?? '',
      llm_configured: llmConfigured,
      agent_running: agentRunning,
    })
  },
  getAccount: () =>
    // available 由 paperEquity 派生，避免设置金额后账户概览自相矛盾
    reply({ equity: paperEquity, available: Math.max(0, Math.round((paperEquity - 3_527.16) * 100) / 100), unrealised_pnl: 255.6 }),
  getPositions: () => reply(positions.map((p) => ({ ...p }))),
  getPortfolio: () => reply(buildPortfolio()),
  getOpenOrders: () => reply(openOrders.map((order) => ({ ...order }))),
  getAlerts: () => reply(priceAlerts.map((alert) => ({ ...alert }))),
  getRounds: (offset, limit) =>
    reply({ items: rounds.slice(offset, offset + limit), total: rounds.length, offset, limit }),
  getRound: (roundId) => {
    // 复盘报告/手动复盘引用的审计轮不在决策轮假数据内（演示 ID 如 9f3ab2…/rv-mock-N）：
    // 回退到通用观望叙事构造详情而非报错，保证复盘工具链内嵌演示可用
    const meta = rounds.find((r) => r.round_id === roundId) ?? {
      round_id: roundId,
      started_at: new Date(Date.now() - 3600_000).toISOString(),
      wake_source: '复盘',
      summary: '复盘审计轮（演示数据）：无归属成交，按观望叙事构造通用工具链。',
      strategyMd5: '',
    }
    return reply(buildRoundDetail(meta))
  },
  // 样例：一条已完成决策轮（3 次工具调用），in_round=false 对应"上轮决策"展示
  getAgentLive: () => reply(buildAgentLive()),
  getTrades: (offset, limit, contract) => {
    const list = [...trades].reverse().filter((t) => !contract || t.contract === contract)
    return reply({ items: list.slice(offset, offset + limit), total: list.length, offset, limit })
  },
  getCandles: (contract, interval, limit = 200) => reply(buildCandles(contract, interval, limit)),
  // 指标配置与序列由 mockIndicators.ts 实现（同一 ApiClient 契约形态）
  getIndicatorConfig: indicatorMock.getIndicatorConfig,
  getIndicatorSeries: indicatorMock.getIndicatorSeries,
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
  getPlan: () =>
    reply({
      content:
        '## BTC 做空\n入场：反弹至 64200-64300 受阻\n止损：64500；目标：63800→63666\n仓位：权益 10%，杠杆 3x\n条件：15m 转阴且量能萎缩\n\n## ETH\n观望，等回踩 1900 整数关确认',
      roundId: rounds[0].round_id,
      updatedAt: new Date(Date.now() - 1800_000).toISOString(),
    }),
  // 固定值：与 mock 成交叙事自洽（当日若干笔已实现合计），上限随风控配置联动
  getDailyStats: () =>
    reply({ realized_pnl: 41.37, orders_today: 7, max_orders_per_day: config.risk.max_orders_per_day }),
  getConfig: () => reply(structuredClone(config)),
  putConfig: (next) => {
    Object.assign(config, next)
    // llm.credentials 整体替换时同步密钥状态列表：key_configured 按名保留，used_by 按 agents 分配重算
    if (next.llm.credentials) {
      credentials = next.llm.credentials.map((c) => ({
        name: c.name,
        provider: c.provider,
        model: c.model,
        api_key_env: c.api_key_env,
        key_configured: credentials.find((o) => o.name === c.name)?.key_configured ?? false,
        used_by: [
          ...(next.agents?.trader?.credential === c.name ? ['trader'] : []),
          ...(next.agents?.reviewer?.credential === c.name ? ['reviewer'] : []),
          ...(next.agents?.researcher?.credential === c.name ? ['researcher'] : []),
        ],
      }))
    }
    // 与后端契约对齐：{saved, needs_restart} + llm 热键（恒按变更处理）两键
    return reply({ saved: true, needs_restart: [], llm_configured: llmConfigured, llm_error: '' })
  },
  getStrategy: () => reply(strategy),
  putStrategy: (content) => {
    strategy = content
    reviewMock.addHumanVersion(content) // 与后端 StrategyStore 对齐：人工保存即落 human 新版本
    return reply(strategy)
  },
  getWatchlist: () => reply(structuredClone(watchlist)),
  putWatchlist: (list) => {
    watchlist.contracts = list.contracts
    watchlist.settle = list.settle
    return reply(structuredClone(watchlist))
  },
  getSecretsStatus: () =>
    reply({
      gate_key: true,
      llm_key: llmConfigured,
      telegram: false,
      credentials: credentials.map((c) => ({ ...c, used_by: [...c.used_by] })),
    }),
  setSecrets: (body) => {
    // 契约：空串/缺省 = 不改动；任一 key 非空则视为已配置（mock 无法模拟失败，error 恒空）
    const anthropic = body.anthropic_api_key ?? ''
    const openai = body.openai_api_key ?? ''
    if (anthropic !== '' || openai !== '') llmConfigured = true
    // 契约扩展：{credential, api_key} 按凭证名写 key；未知凭证 422（与后端一致）
    if (body.credential && body.api_key) {
      const target = credentials.find((c) => c.name === body.credential)
      if (!target) return Promise.reject(new ApiError(422, `未知凭证: ${body.credential}`))
      target.key_configured = true
      llmConfigured = true
    }
    return reply({ saved: true, llm_configured: llmConfigured, error: '' })
  },
  // 契约：重名 422；api_key_env 按后端规则由 name 推导（与 CredentialForm 的 deriveEnv 一致）；
  // 密钥状态 credentials 与 config.llm.credentials 两份内存数据同步追加，保证回显自洽
  createCredential: (body) => {
    if (credentials.some((c) => c.name === body.name)) {
      return Promise.reject(new ApiError(422, `凭证已存在: ${body.name}`))
    }
    const key = body.api_key ?? ''
    const apiKeyEnv = `LLM_KEY_${body.name.toUpperCase().replace(/-/g, '_')}`
    credentials.push({
      name: body.name,
      provider: body.provider,
      model: body.model,
      api_key_env: apiKeyEnv,
      key_configured: key !== '',
      used_by: [],
    })
    config.llm.credentials?.push({
      name: body.name,
      provider: body.provider,
      model: body.model,
      max_tokens: body.max_tokens,
      openai_base_url: body.openai_base_url,
      thinking_effort: body.thinking_effort ?? '',
      api_key_env: apiKeyEnv,
    })
    if (key !== '') llmConfigured = true
    return reply({ saved: true, key_saved: key !== '', llm_configured: true, llm_error: '' })
  },
  // 契约：未知名 404；改 provider/model/max_tokens/openai_base_url，api_key_env 保持不变；
  // api_key 空串/缺省 = 不动 key（同 setSecrets 防护），非空则视为已配置
  updateCredential: (name, body) => {
    const target = credentials.find((c) => c.name === name)
    if (!target) return Promise.reject(new ApiError(404, `未知凭证: ${name}`))
    target.provider = body.provider
    target.model = body.model
    const key = body.api_key ?? ''
    if (key !== '') {
      target.key_configured = true
      llmConfigured = true
    }
    const defined = config.llm.credentials?.find((c) => c.name === name)
    if (defined) {
      defined.provider = body.provider
      defined.model = body.model
      defined.max_tokens = body.max_tokens
      defined.openai_base_url = body.openai_base_url
      defined.thinking_effort = body.thinking_effort ?? ''
    }
    return reply({ saved: true, key_saved: key !== '', llm_configured: true, llm_error: '' })
  },
  // 契约：未知名 404；被 agents 引用（used_by 非空）422；.env 里的 key 保留不删（与后端一致）
  deleteCredential: (name) => {
    const index = credentials.findIndex((c) => c.name === name)
    if (index < 0) return Promise.reject(new ApiError(404, `未知凭证: ${name}`))
    if (credentials[index].used_by.length > 0) {
      return Promise.reject(new ApiError(422, `凭证仍被引用: ${name}`))
    }
    credentials.splice(index, 1)
    if (config.llm.credentials) {
      config.llm.credentials = config.llm.credentials.filter((c) => c.name !== name)
    }
    return reply({ saved: true, key_saved: false, llm_configured: true, llm_error: '' })
  },
  setKillSwitch: (enabled) => {
    killSwitch = enabled
    return reply({ kill_switch: killSwitch })
  },
  // 复盘/策略版本方法由 mockReview.ts 实现（同一 ApiClient 契约形态）
  getReviewReports: reviewMock.handlers.getReviewReports,
  getReviewReport: reviewMock.handlers.getReviewReport,
  runReview: reviewMock.handlers.runReview,
  getReviewLive: reviewMock.handlers.getReviewLive,
  getStrategyVersions: reviewMock.handlers.getStrategyVersions,
  getStrategyVersion: reviewMock.handlers.getStrategyVersion,
  getStrategyDiff: reviewMock.handlers.getStrategyDiff,
  rollbackStrategy: reviewMock.handlers.rollbackStrategy,
  // 研报方法由 mockResearch.ts 实现（同一 ApiClient 契约形态）
  getResearchReports: researchMock.handlers.getResearchReports,
  getResearchReport: researchMock.handlers.getResearchReport,
  runResearch: researchMock.handlers.runResearch,
  getResearchLive: researchMock.handlers.getResearchLive,
  // 按 agent 转发三实现并归一为 LiveSnapshot（mock 行为与现状一致：trader 上轮、复盘/研报无进行中轮）
  getLiveFor: (agent): Promise<LiveSnapshot> => {
    switch (agent) {
      case 'trader':
        return reply(buildAgentLive())
      case 'review':
        return reviewMock.handlers.getReviewLive()
      case 'research':
        return researchMock.handlers.getResearchLive()
    }
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
  return { round_id: meta.round_id, prompt_snapshot: promptSnapshot(meta), strategyMd5: meta.strategyMd5, ...body }
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
