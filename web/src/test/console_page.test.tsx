/**
 * ConsolePage 冒烟 + WS 联动测试：mock api + 可控 useWs 后整页渲染，
 * 断言关键区域全部就位——TopBar / 账户面板 / 实时决策轮主角 / K线 / 持仓 /
 * 决策时间线 / Agent 笔记 / 成交记录 / 配置抽屉（RoundFocusProvider 由页面内部包裹）；
 * 并验证 WS round 事件触发账户、持仓、权益与笔记面板（唯一笔记消费者）的刷新。
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import type { AgentLiveState, AppConfig, LiveAgentKind, LiveSnapshot, OpenOrder, PriceAlert, RoundDetail, StatusInfo, WsMessage } from '../api/types'
import ConsolePage from '../pages/ConsolePage'

const STATUS: StatusInfo = {
  mode: 'paper',
  uptime_seconds: 3600,
  kill_switch: false,
  llm_credential_name: 'default',
  llm_provider: 'anthropic',
  llm_model: 'claude-sonnet',
  llm_thinking_effort: '',
  llm_configured: true,
  agent_running: true,
}

/** 配置夹具：RiskPanel / 当日统计（max_orders_per_day）消费 */
const CONFIG: AppConfig = {
  mode: 'paper',
  llm: {
    provider: 'anthropic',
    model: 'claude-sonnet-4-5',
    max_tokens: 4096,
    openai_base_url: '',
    thinking_effort: '',
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

/** 实时轮夹具：上一轮已结束（触发 hero 拉取审计详情） */
const LIVE: AgentLiveState = {
  in_round: false,
  round: {
    round_id: 'r-smoke-1',
    wake_source: '定时唤醒',
    prompt_md5: 'md5',
    prompt_snapshot: 'prompt',
    context_snapshot: 'ctx',
    llm_raw: '',
    started_at: 1_700_000_000,
    ended_at: 1_700_000_300,
    error: '',
  },
  tool_calls: [],
}

const DETAIL: RoundDetail = {
  round_id: 'r-smoke-1',
  prompt_snapshot: 'prompt',
  context_snapshot: 'ctx',
  llm_raw: 'RAW-SMOKE',
  tool_calls: [],
  strategyMd5: 'md5',
}

/** 可变数仓：WS 消息（测试中途改写触发联动）+ 联动查询的调用计数 */
const holder = vi.hoisted(() => ({
  lastMessage: null as WsMessage | null,
  getStatus: vi.fn(() => Promise.resolve(STATUS)),
  getPortfolio: vi.fn(() =>
    Promise.resolve({
      asOf: '2026-07-20T00:00:01Z',
      account: { equity: 10284.56, available: 9216.36, unrealised_pnl: 133.13 },
      positions: [],
    }),
  ),
  getOpenOrders: vi.fn<() => Promise<OpenOrder[]>>(() => Promise.resolve([])),
  getAlerts: vi.fn<() => Promise<PriceAlert[]>>(() => Promise.resolve([])),
  cancelOpenOrder: vi.fn(),
  getEquity: vi.fn(() =>
    Promise.resolve({
      initialEquity: 10000,
      baselineSource: 'paper_config',
      points: [{ time: '2026-07-19T00:00:00Z', equity: 10000 }],
    }),
  ),
  getNotes: vi.fn(() =>
    Promise.resolve({
      items: [{ time: '2026-07-20T00:00:00Z', content: '自检笔记', round_id: '' }],
      total: 1,
      offset: 0,
      limit: 4,
    }),
  ),
  getDailyStats: vi.fn(() =>
    Promise.resolve({ realized_pnl: 41.37, orders_today: 7, max_orders_per_day: 20 }),
  ),
  getStrategy: vi.fn(() => Promise.resolve('# 系统提示词')),
  // 交易计划面板（左栏，策略下方）数据源：计数可断言（plan_updated 联动测试用）
  getPlan: vi.fn(() =>
    Promise.resolve({ content: '## BTC 做空\n入场：反弹受阻', roundId: 'r1', updatedAt: '2026-07-20T00:00:00Z' }),
  ),
  getAgentLive: vi.fn<() => Promise<AgentLiveState>>(() => Promise.resolve(LIVE)),
}))

vi.mock('../api', () => ({
  api: {
    getStatus: () => holder.getStatus(),
    getPortfolio: () => holder.getPortfolio(),
    getOpenOrders: () => holder.getOpenOrders(),
    getAlerts: () => holder.getAlerts(),
    cancelOpenOrder: (...args: [string, string]) => holder.cancelOpenOrder(...args),
    getEquity: () => holder.getEquity(),
    getNotes: () => holder.getNotes(),
    getDailyStats: () => holder.getDailyStats(),
    getAgentLive: () => holder.getAgentLive(),
    // 实时决策轮主角（多 agent 改造后）数据源：trader 走 getAgentLive 夹具并归一形状；复盘/研报恒无轮次
    getLiveFor: (agent: LiveAgentKind): Promise<LiveSnapshot> =>
      agent === 'trader'
        ? holder.getAgentLive().then((s) => ({ round: s.round, tool_calls: s.tool_calls }))
        : Promise.resolve({ round: null, tool_calls: [] }),
    getRound: () => Promise.resolve(DETAIL),
    getRounds: () => Promise.resolve({ items: [], total: 0, offset: 0, limit: 5 }),
    getTrades: () => Promise.resolve({ items: [], total: 0, offset: 0, limit: 20 }),
    // 复盘报告面板与决策时间线版本标签数据源
    getReviewReports: () => Promise.resolve({ items: [], total: 0 }),
    // 复盘进行中进度条数据源：无进行中复盘轮（进度条保持隐藏）
    getReviewLive: () => Promise.resolve({ round: null, tool_calls: [] }),
    // 研报面板数据源（与复盘并排）
    getResearchReports: () => Promise.resolve({ items: [], total: 0 }),
    // 研报进行中进度条数据源：无进行中研报轮（进度条保持隐藏）
    getResearchLive: () => Promise.resolve({ round: null, tool_calls: [] }),
    getStrategyVersions: () => Promise.resolve([]),
    getConfig: () => Promise.resolve(CONFIG),
    getWatchlist: () => Promise.resolve({ settle: 'USDT', contracts: ['BTC_USDT'] }),
    getCandles: () => Promise.resolve([]),
    // 指标面板数据源：空短名单（K线不叠加指标线、徽标条不渲染）
    getIndicatorConfig: () => Promise.resolve({ shortlist: [], available: [] }),
    getIndicatorSeries: () => Promise.resolve({ contract: 'BTC_USDT', interval: '1h', series: {} }),
    // 配置抽屉数据源与 paper 重置（重置联动测试用）；策略面板复用 getStrategy/getStrategyVersions
    getStrategy: () => holder.getStrategy(),
    getStrategyVersion: (id: number) =>
      Promise.resolve({
        id,
        md5: 'md5',
        createdBy: 'human',
        reason: '',
        reportId: null,
        time: '2026-07-20T00:00:00Z',
        content: '# 旧版本',
      }),
    getSecretsStatus: () => Promise.resolve({ gate_key: true, llm_key: true, telegram: false }),
    resetPaperEquity: (equity: number) => Promise.resolve({ equity }),
    // 交易计划面板（左栏，策略下方）数据源
    getPlan: () => holder.getPlan(),
  },
  // 写操作按钮（AgentControl/PositionsPanel 等）catch 分支做 instanceof ApiError，mock 必须透出该类
  ApiError: class ApiError extends Error {},
}))
// 隔离真实 WS（jsdom 无 WebSocket）：消息经 holder.lastMessage 可控派发
vi.mock('../hooks/useWs', () => ({
  useWs: () => ({ connected: true, lastMessage: holder.lastMessage }),
}))
// jsdom 无 canvas：lightweight-charts 换最小桩（K线创建/序列/坐标换算全空操作）
vi.mock('lightweight-charts', () => ({
  ColorType: { Solid: 'solid' },
  CandlestickSeries: 'candlestick',
  HistogramSeries: 'histogram',
  LineSeries: 'line',
  createChart: () => ({
    addSeries: () => ({
      setData: vi.fn(),
      update: vi.fn(),
      priceToCoordinate: () => null,
      priceScale: () => ({
        options: () => ({ scaleMargins: { top: 0.08, bottom: 0.26 } }),
      }),
      getPane: () => ({ getHeight: () => 300 }),
      subscribeDataChanged: vi.fn(),
      unsubscribeDataChanged: vi.fn(),
    }),
    priceScale: () => ({ applyOptions: vi.fn() }),
    timeScale: () => ({
      setVisibleLogicalRange: vi.fn(),
      subscribeVisibleLogicalRangeChange: vi.fn(),
      unsubscribeVisibleLogicalRangeChange: vi.fn(),
      subscribeSizeChange: vi.fn(),
      unsubscribeSizeChange: vi.fn(),
      timeToCoordinate: () => null,
      width: () => 400,
    }),
    applyOptions: vi.fn(),
    subscribeClick: vi.fn(),
    unsubscribeClick: vi.fn(),
    remove: vi.fn(),
    // 指标挂接（useIndicatorChart）使用的系列/副图管理方法
    removeSeries: vi.fn(),
    addPane: () => ({ paneIndex: () => 1, setStretchFactor: vi.fn() }),
    panes: () => [{}],
    removePane: vi.fn(),
  }),
}))

beforeAll(() => {
  // jsdom 未实现 ResizeObserver / scrollIntoView，注入桩
  window.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver
  window.HTMLElement.prototype.scrollIntoView = vi.fn()
})

beforeEach(() => {
  vi.clearAllMocks()
  holder.lastMessage = null
})

describe('ConsolePage(AI 大脑观察舱)', () => {
  it('整页渲染冒烟：TopBar/账户/实时轮/K线/持仓/时间线/笔记/成交记录关键区域齐备', async () => {
    render(<ConsolePage />)

    // TopBar：品牌副标题 + mode 徽标 + 配置入口
    expect(screen.getByText(/AI 大脑观察舱/)).toBeInTheDocument()
    expect(await screen.findByText('PAPER · 模拟盘')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '打开配置中心' })).toBeInTheDocument()
    // 左栏：账户面板 + 权益曲线
    expect(await screen.findByText(/账户 · PAPER/)).toBeInTheDocument()
    // 权益大数字（账户面板与权益曲线末值各出现一次）
    expect(screen.getAllByText('10,284.56').length).toBeGreaterThan(0)
    // 账户面板增强：累计涨跌行（equity 夹具 10000 → 10284.56 = +2.85%）
    expect(screen.getByText('▲ +2.85% · 累计')).toBeInTheDocument()
    // 权益小曲线标题行同款涨跌幅
    expect(screen.getByText('+2.85%')).toBeInTheDocument()
    // 账户面板底部行：今日已实现 +41.37 · 当日开仓单 7/20（后端 /api/daily_stats 口径，异步等待）
    expect(await screen.findByText('今日已实现')).toBeInTheDocument()
    expect(screen.getByText('当日开仓单')).toBeInTheDocument()
    expect(screen.getByText('/20')).toBeInTheDocument()
    // 硬性风控速览（config 夹具 max_position_pct 0.3 → 30%，独立查询，异步等待）
    expect(screen.getByText('硬性风控 · 代码保证')).toBeInTheDocument()
    expect(await screen.findByText('30%')).toBeInTheDocument()
    // 左栏：策略面板（只读）——标题、当前策略全文（getStrategy 夹具）、去配置中心入口
    expect(screen.getByText('策略 · system_prompt')).toBeInTheDocument()
    expect(await screen.findByText(/系统提示词/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '去配置中心修改' })).toBeInTheDocument()
    // 左栏：交易计划面板（策略下方，只读）——标题与计划全文（getPlan 夹具）
    expect(screen.getByText('交易计划 · trade_plan')).toBeInTheDocument()
    expect(await screen.findByText(/反弹受阻/)).toBeInTheDocument()
    // 中央：实时决策轮主角（结束后拉详情，渲染结论；结论与对话流各出现一次）
    expect(await screen.findByText(/实时决策轮/)).toBeInTheDocument()
    expect(document.body).not.toHaveTextContent(/started_at\(开始\)|ended_at\(结束\)/)
    expect((await screen.findAllByText('RAW-SMOKE')).length).toBeGreaterThan(0)
    // 右栏：K线 + 空仓持仓面板
    expect(screen.getByText('K线')).toBeInTheDocument()
    expect(screen.getByText('当前无持仓')).toBeInTheDocument()
    expect(screen.getByText('当前无价格唤醒')).toBeInTheDocument() // 挂单下方的新面板
    // 第二屏：决策时间线 + Agent 笔记
    expect(screen.getByText(/决策时间线/)).toBeInTheDocument()
    expect(screen.getByText('Agent 笔记')).toBeInTheDocument()
    expect(screen.getByText('自检笔记')).toBeInTheDocument()
    // 研报面板 + 复盘报告面板（并排各半，成交记录之前）
    expect(screen.getByRole('button', { name: '生成研报' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '立即复盘' })).toBeInTheDocument()
    // 第三屏：成交记录
    expect(screen.getByText('成交记录')).toBeInTheDocument()
    // 配置抽屉：默认关闭（dialog 存在但不可见）
    expect(screen.getByRole('dialog', { hidden: true })).toHaveAttribute('aria-hidden', 'true')
  })

  it('进行中的决策轮保留 null 技术状态及中文补充', async () => {
    // 持久 mock（非 Once）：useLiveAgent 挂载补漏与 hero 数据查询都会各调一次 getLiveFor('trader')，两次都需返回进行中轮；
    // started_at 取当前时间（30 分钟僵尸阈值内），否则会被当作崩溃残留的僵尸轮、不显示进行中态
    holder.getAgentLive.mockResolvedValue({
      ...LIVE,
      in_round: true,
      round:
        LIVE.round === null
          ? null
          : { ...LIVE.round, started_at: Math.floor(Date.now() / 1000) - 10, ended_at: null },
    })

    render(<ConsolePage />)

    expect(await screen.findByText('null（进行中）')).toBeInTheDocument()
  })

  it('WS round 事件 → account/positions/equity/dailyStats 刷新，笔记面板同步失效', async () => {
    const { rerender } = render(<ConsolePage />)
    await screen.findByText(/账户 · PAPER/)
    expect(holder.getPortfolio).toHaveBeenCalledTimes(1)
    expect(holder.getOpenOrders).toHaveBeenCalledTimes(1)

    // 后端广播轮结束（payload 仅 {round_id, ok, wake_source}，装配层只当失效信号）
    holder.lastMessage = {
      type: 'round',
      data: { round_id: 'r-smoke-2', ok: true, wake_source: '价格触发' },
    }
    rerender(<ConsolePage />)

    await waitFor(() => expect(holder.getPortfolio).toHaveBeenCalledTimes(2))
    expect(holder.getOpenOrders).toHaveBeenCalledTimes(2)
    expect(holder.getEquity).toHaveBeenCalledTimes(2)
    // notes 仅 NotesPanel 2 次（挂载 + round 事件刷新）；时间线引文随 getRounds 当前页下发，不再独立拉取
    expect(holder.getNotes).toHaveBeenCalledTimes(2)
    // 当日统计同步联动（新轮成交改变当日口径）
    expect(holder.getDailyStats).toHaveBeenCalledTimes(2)
    // 价格唤醒联动刷新（LLM 设置/触发唤醒均伴随决策轮事件）
    expect(holder.getAlerts).toHaveBeenCalledTimes(2)
    // 策略面板联动刷新（新决策轮可能由新策略版本驱动）
    expect(holder.getStrategy).toHaveBeenCalledTimes(2)
  })

  it('WS strategy_updated / plan_updated → 对应面板即时重拉（不等决策轮事件）', async () => {
    const { rerender } = render(<ConsolePage />)
    await screen.findByText(/账户 · PAPER/)
    await screen.findByText(/反弹受阻/)
    const strategyBase = holder.getStrategy.mock.calls.length
    const planBase = holder.getPlan.mock.calls.length

    // 复盘 agent 修订策略落版本即推：仅策略面板重拉，计划面板不动
    holder.lastMessage = { type: 'strategy_updated' }
    rerender(<ConsolePage />)
    await waitFor(() => expect(holder.getStrategy.mock.calls.length).toBe(strategyBase + 1))
    expect(holder.getPlan.mock.calls.length).toBe(planBase)

    // 执行 agent 工具改完计划轮中即推：仅计划面板重拉，策略面板不动
    holder.lastMessage = { type: 'plan_updated' }
    rerender(<ConsolePage />)
    await waitFor(() => expect(holder.getPlan.mock.calls.length).toBe(planBase + 1))
    expect(holder.getStrategy.mock.calls.length).toBe(strategyBase + 1)
  })

  it('关闭配置抽屉 → 顶部状态与策略面板同时重拉', async () => {
    render(<ConsolePage />)
    await screen.findByText(/账户 · PAPER/)

    // 打开抽屉（抽屉自身会拉取策略作为 StrategyEditor 数据源），等其就绪后记录计数基线
    fireEvent.click(screen.getByRole('button', { name: '打开配置中心' }))
    await screen.findByLabelText('system_prompt 内容')
    const beforeClose = holder.getStrategy.mock.calls.length
    const statusBeforeClose = holder.getStatus.mock.calls.length

    fireEvent.click(screen.getByRole('button', { name: '关闭配置中心' }))

    // 关闭后重新获取状态，确保凭证编辑立即反映到 TopBar；策略面板也同步重拉。
    await waitFor(() => expect(holder.getStatus.mock.calls.length).toBe(statusBeforeClose + 1))
    await waitFor(() => expect(holder.getStrategy.mock.calls.length).toBe(beforeClose + 1))
  })


describe('ConsolePage 挂单刷新', () => {
  it('撤单成功后同时重新请求 openOrders 与 account', async () => {
    holder.getOpenOrders
      .mockResolvedValueOnce([
        {
          id: 'order-refresh',
          contract: 'ETH_USDT',
          size: 79,
          left: 79,
          price: 1900,
          tif: 'gtc',
          reduce_only: false,
          status: 'open',
          stop_loss_price: null,
          take_profit_price: null,
        },
      ])
      .mockResolvedValue([])
    holder.cancelOpenOrder.mockResolvedValue({
      id: 'order-refresh',
      contract: 'ETH_USDT',
      status: 'finished',
      finish_as: 'cancelled',
      warning: '',
    })

    render(<ConsolePage />)
    await screen.findByText('ETH_USDT')

    fireEvent.click(screen.getByRole('button', { name: '手动撤单' }))
    fireEvent.click(screen.getByRole('button', { name: '再次点击确认撤单' }))

    await waitFor(() => expect(holder.cancelOpenOrder).toHaveBeenCalledWith('ETH_USDT', 'order-refresh'))
    await waitFor(() => expect(holder.getOpenOrders).toHaveBeenCalledTimes(2))
    expect(holder.getPortfolio).toHaveBeenCalledTimes(2)
  })
})


  it('paper 重置权益 → account/positions/equity/dailyStats 四路联动刷新', async () => {
    render(<ConsolePage />)
    await screen.findByText(/账户 · PAPER/)
    expect(holder.getDailyStats).toHaveBeenCalledTimes(1)

    // 打开配置抽屉（paper 模式渲染权益重置），两段确认完成重置
    fireEvent.click(screen.getByRole('button', { name: '打开配置中心' }))
    expect(await screen.findByText('设置权益金额 USDT')).toBeInTheDocument()
    expect(screen.queryByText(/equity\(设置权益金额/)).not.toBeInTheDocument()
    fireEvent.click(await screen.findByRole('button', { name: '设置金额' }))
    fireEvent.click(await screen.findByRole('button', { name: /再次点击确认/ }))

    await waitFor(() => expect(holder.getPortfolio).toHaveBeenCalledTimes(2))
    expect(holder.getEquity).toHaveBeenCalledTimes(2)
    expect(holder.getDailyStats).toHaveBeenCalledTimes(2)
  })
})
