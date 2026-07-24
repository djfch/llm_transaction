/**
 * ConsolePage 冒烟 + WS 联动测试：mock api + 可控 useWs 后整页渲染，
 * 断言关键区域全部就位——TopBar / 账户面板 / 实时决策轮主角 / K线 / 持仓 /
 * 决策时间线 / Agent 笔记 / 成交记录 / 配置抽屉（RoundFocusProvider 由页面内部包裹）；
 * 并验证 WS round 事件触发账户、持仓、权益与两个笔记消费者的刷新。
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import type { AgentLiveState, AppConfig, OpenOrder, RoundDetail, StatusInfo, WsMessage } from '../api/types'
import ConsolePage from '../pages/ConsolePage'

const STATUS: StatusInfo = {
  mode: 'paper',
  uptime_seconds: 3600,
  kill_switch: false,
  llm_provider: 'anthropic',
  llm_model: 'claude-sonnet',
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
  llm_raw: 'RAW-SMOKE',
  tool_calls: [],
}

/** 可变数仓：WS 消息（测试中途改写触发联动）+ 联动查询的调用计数 */
const holder = vi.hoisted(() => ({
  lastMessage: null as WsMessage | null,
  getAccount: vi.fn(() =>
    Promise.resolve({ equity: 10284.56, available: 9216.36, unrealised_pnl: 133.13 }),
  ),
  getPositions: vi.fn(() => Promise.resolve([])),
  getOpenOrders: vi.fn<() => Promise<OpenOrder[]>>(() => Promise.resolve([])),
  cancelOpenOrder: vi.fn(),
  getEquity: vi.fn(() =>
    Promise.resolve([
      { time: '2026-07-19T00:00:00Z', equity: 10000 },
      { time: '2026-07-20T00:00:00Z', equity: 10284.56 },
    ]),
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
  getAgentLive: vi.fn<() => Promise<AgentLiveState>>(() => Promise.resolve(LIVE)),
}))

vi.mock('../api', () => ({
  api: {
    getStatus: () => Promise.resolve(STATUS),
    getAccount: () => holder.getAccount(),
    getPositions: () => holder.getPositions(),
    getOpenOrders: () => holder.getOpenOrders(),
    cancelOpenOrder: (...args: [string, string]) => holder.cancelOpenOrder(...args),
    getEquity: () => holder.getEquity(),
    getNotes: () => holder.getNotes(),
    getDailyStats: () => holder.getDailyStats(),
    getAgentLive: () => holder.getAgentLive(),
    getRound: () => Promise.resolve(DETAIL),
    getRounds: () => Promise.resolve({ items: [], total: 0, offset: 0, limit: 5 }),
    getTrades: () => Promise.resolve({ items: [], total: 0, offset: 0, limit: 20 }),
    getConfig: () => Promise.resolve(CONFIG),
    getWatchlist: () => Promise.resolve({ settle: 'USDT', contracts: ['BTC_USDT'] }),
    getCandles: () => Promise.resolve([]),
    // 配置抽屉数据源与 paper 重置（重置联动测试用）
    getStrategy: () => Promise.resolve('# 系统提示词'),
    getSecretsStatus: () => Promise.resolve({ gate_key: true, llm_key: true, telegram: false }),
    resetPaperEquity: (equity: number) => Promise.resolve({ equity }),
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
    addSeries: () => ({ setData: vi.fn(), update: vi.fn(), priceToCoordinate: () => null }),
    priceScale: () => ({ applyOptions: vi.fn() }),
    timeScale: () => ({
      setVisibleLogicalRange: vi.fn(),
      subscribeVisibleLogicalRangeChange: vi.fn(),
      unsubscribeVisibleLogicalRangeChange: vi.fn(),
      timeToCoordinate: () => null,
    }),
    applyOptions: vi.fn(),
    remove: vi.fn(),
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
    // 中央：实时决策轮主角（结束后拉详情，渲染结论；结论与对话流各出现一次）
    expect(await screen.findByText(/实时决策轮/)).toBeInTheDocument()
    expect(document.body).not.toHaveTextContent(/started_at\(开始\)|ended_at\(结束\)/)
    expect((await screen.findAllByText('RAW-SMOKE')).length).toBeGreaterThan(0)
    // 右栏：K线 + 空仓持仓面板
    expect(screen.getByText('K线')).toBeInTheDocument()
    expect(screen.getByText('当前无持仓')).toBeInTheDocument()
    // 第二屏：决策时间线 + Agent 笔记
    expect(screen.getByText(/决策时间线/)).toBeInTheDocument()
    expect(screen.getByText('Agent 笔记')).toBeInTheDocument()
    expect(screen.getByText('自检笔记')).toBeInTheDocument()
    // 第三屏：成交记录
    expect(screen.getByText('成交记录')).toBeInTheDocument()
    // 配置抽屉：默认关闭（dialog 存在但不可见）
    expect(screen.getByRole('dialog', { hidden: true })).toHaveAttribute('aria-hidden', 'true')
  })

  it('进行中的决策轮保留 null 技术状态及中文补充', async () => {
    holder.getAgentLive.mockResolvedValueOnce({
      ...LIVE,
      in_round: true,
      round: LIVE.round === null ? null : { ...LIVE.round, ended_at: null },
    })

    render(<ConsolePage />)

    expect(await screen.findByText('null（进行中）')).toBeInTheDocument()
  })

  it('WS round 事件 → account/positions/equity/dailyStats 刷新，笔记面板与引文同步失效', async () => {
    const { rerender } = render(<ConsolePage />)
    await screen.findByText(/账户 · PAPER/)
    expect(holder.getAccount).toHaveBeenCalledTimes(1)
    expect(holder.getPositions).toHaveBeenCalledTimes(1)
    expect(holder.getOpenOrders).toHaveBeenCalledTimes(1)

    // 后端广播轮结束（payload 仅 {round_id, ok, wake_source}，装配层只当失效信号）
    holder.lastMessage = {
      type: 'round',
      data: { round_id: 'r-smoke-2', ok: true, wake_source: '价格触发' },
    }
    rerender(<ConsolePage />)

    await waitFor(() => expect(holder.getAccount).toHaveBeenCalledTimes(2))
    expect(holder.getPositions).toHaveBeenCalledTimes(2)
    expect(holder.getOpenOrders).toHaveBeenCalledTimes(2)
    expect(holder.getEquity).toHaveBeenCalledTimes(2)
    // notes = NotesPanel 与 RoundTimeline 引文各 2 次（挂载 + round 事件刷新）
    expect(holder.getNotes).toHaveBeenCalledTimes(4)
    // 当日统计同步联动（新轮成交改变当日口径）
    expect(holder.getDailyStats).toHaveBeenCalledTimes(2)
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
    expect(holder.getAccount).toHaveBeenCalledTimes(2)
  })
})


  it('paper 重置权益 → account/positions/equity/dailyStats 四路联动刷新（回归 M3）', async () => {
    render(<ConsolePage />)
    await screen.findByText(/账户 · PAPER/)
    expect(holder.getDailyStats).toHaveBeenCalledTimes(1)

    // 打开配置抽屉（paper 模式渲染权益重置），两段确认完成重置
    fireEvent.click(screen.getByRole('button', { name: '打开配置中心' }))
    expect(await screen.findByText('设置权益金额 USDT')).toBeInTheDocument()
    expect(screen.queryByText(/equity\(设置权益金额/)).not.toBeInTheDocument()
    fireEvent.click(await screen.findByRole('button', { name: '设置金额' }))
    fireEvent.click(await screen.findByRole('button', { name: /再次点击确认/ }))

    await waitFor(() => expect(holder.getAccount).toHaveBeenCalledTimes(2))
    expect(holder.getPositions).toHaveBeenCalledTimes(2)
    expect(holder.getEquity).toHaveBeenCalledTimes(2)
    expect(holder.getDailyStats).toHaveBeenCalledTimes(2)
  })
})
