/**
 * 实时决策轮多 agent 测试：useLiveAgent 活跃栈状态机（后到优先/结束回切/全部结束停留）、
 * 合并式补漏（挂载与断线重连 connected false→true 触发，与 WS 先行入栈项去重合并）、
 * 僵尸轮过滤（ended_at=null 但 started_at 超 30 分钟的崩溃残留轮不算进行中）、
 * 结束后 live 返回带服务器 ended_at 的终态轮（三端点统一保留终态轮）+ 3 秒轮询与六 WS 事件联动。
 * WS 模拟参照 research_live_strip.test.tsx：mock ../api 模块 + 可控 useWs（holder 派发）。
 */
import { act, render, screen } from '@testing-library/react'
import type { RenderResult } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type {
  AgentLiveRound,
  LiveAgentKind,
  LiveSnapshot,
  RoundDetail,
  StatusInfo,
  WsMessage,
} from '../api/types'
import LiveRoundHero from '../components/console/LiveRoundHero'

const holder = vi.hoisted(() => ({
  connected: true,
  lastMessage: null as WsMessage | null,
  getLiveFor: vi.fn<(agent: LiveAgentKind) => Promise<LiveSnapshot>>(),
  getRound: vi.fn<(roundId: string) => Promise<RoundDetail>>(),
  status: {
    mode: 'paper',
    uptime_seconds: 3600,
    kill_switch: false,
    llm_provider: 'anthropic',
    llm_model: 'claude-sonnet',
    llm_configured: true,
    agent_running: true,
  } as StatusInfo,
}))

vi.mock('../api', () => ({
  api: {
    getLiveFor: (agent: LiveAgentKind) => holder.getLiveFor(agent),
    getRound: (roundId: string) => holder.getRound(roundId),
    getStatus: () => Promise.resolve(holder.status),
  },
}))

vi.mock('../hooks/useWs', () => ({
  useWs: () => ({ connected: holder.connected, lastMessage: holder.lastMessage }),
}))

const NOW_S = 1_784_600_000 // 固定「现在」（Unix 秒），配合 setSystemTime 保证进行中/耗时判定确定

/** 构造一轮实时快照 round（形状与三端点归一后的 AgentLiveRound 一致）。 */
function liveRound(roundId: string, startedAt: number, endedAt: number | null): AgentLiveRound {
  return {
    round_id: roundId,
    wake_source: '定时唤醒',
    prompt_md5: 'md5',
    prompt_snapshot: 'prompt',
    context_snapshot: 'ctx',
    llm_raw: '',
    started_at: startedAt,
    ended_at: endedAt,
    error: '',
  }
}

/** 包装 LiveSnapshot（工具链默认空）。 */
function snap(round: AgentLiveRound | null): LiveSnapshot {
  return { round, tool_calls: [] }
}

const TRADER_ENDED = liveRound('r-trader-1', NOW_S - 600, NOW_S - 300) // trader 上轮（已结束）
const REVIEW_RUNNING = liveRound('r-review-1', NOW_S - 20, null) // 复盘轮（进行中）
const RESEARCH_RUNNING = liveRound('r-research-1', NOW_S - 5, null) // 研报轮（进行中）

/** 冲刷微任务与 0ms 定时器队列（promise → setState → effect → 再请求的链需多轮）。 */
async function flush() {
  for (let i = 0; i < 5; i++) await act(async () => vi.advanceTimersByTimeAsync(0))
}

/** 渲染并冲刷挂载期请求（挂载补漏三端点 + hero 首次查询 + getStatus）。 */
async function renderHero() {
  const utils = render(<LiveRoundHero />)
  await flush()
  return utils
}

/** 派发一条 WS 消息并冲刷后续状态更新与请求。 */
async function sendWs(utils: RenderResult, msg: WsMessage) {
  holder.lastMessage = msg
  utils.rerender(<LiveRoundHero />)
  await flush()
}

/** 切换 connected 并冲刷（断线重连 false→true 会触发 useLiveAgent 重新补漏）。 */
async function setConnected(utils: RenderResult, connected: boolean) {
  holder.connected = connected
  utils.rerender(<LiveRoundHero />)
  await flush()
}

/** 把 hero 推进到「复盘在跑」状态（trader 上轮 → review_round_start 切换）。 */
async function enterReviewRunning(utils: RenderResult) {
  holder.getLiveFor.mockImplementation((agent) =>
    Promise.resolve(agent === 'review' ? snap(REVIEW_RUNNING) : snap(null)),
  )
  await sendWs(utils, { type: 'review_round_start', data: { round_id: 'r-review-1' } })
}

/** 把 hero 推进到「复盘 + 研报同时在跑、当前显示研报」状态（后到优先）。 */
async function enterResearchRunning(utils: RenderResult) {
  await enterReviewRunning(utils)
  holder.getLiveFor.mockImplementation((agent) => {
    if (agent === 'research') return Promise.resolve(snap(RESEARCH_RUNNING))
    if (agent === 'review') return Promise.resolve(snap(REVIEW_RUNNING))
    return Promise.resolve(snap(null))
  })
  await sendWs(utils, { type: 'research_round_start', data: { round_id: 'r-research-1' } })
}

beforeEach(() => {
  vi.useFakeTimers()
  vi.setSystemTime(NOW_S * 1000)
  holder.connected = true
  holder.lastMessage = null
  holder.getLiveFor.mockReset()
  holder.getRound.mockReset()
  // 默认：trader 保留上轮（已结束），复盘/研报无轮次；审计详情返回一条可断言的结论文本
  holder.getLiveFor.mockImplementation((agent) =>
    Promise.resolve(agent === 'trader' ? snap(TRADER_ENDED) : snap(null)),
  )
  holder.getRound.mockImplementation((roundId) =>
    Promise.resolve({
      round_id: roundId,
      prompt_snapshot: 'prompt',
      llm_raw: 'RAW-CONCLUSION',
      tool_calls: [],
      strategyMd5: '',
    }),
  )
})

afterEach(() => {
  vi.useRealTimers()
})

describe('LiveRoundHero(实时决策轮多 agent)', () => {
  it('默认无进行中：显示 trader 上轮决策轮与「交易」「上轮决策」徽标', async () => {
    await renderHero()
    expect(screen.getByText('#r-trader-1')).toBeInTheDocument()
    expect(screen.getByText('交易')).toBeInTheDocument()
    expect(screen.getByText('上轮决策')).toBeInTheDocument()
    expect(screen.getByText('定时唤醒')).toBeInTheDocument() // wake_source 徽标仅 trader 显示
    expect(holder.getLiveFor).toHaveBeenCalledWith('trader')
  })

  it('trader 轮进行中：显示进行中态并每 3 秒轮询 getLiveFor(trader)', async () => {
    holder.getLiveFor.mockImplementation((agent) =>
      Promise.resolve(agent === 'trader' ? snap(liveRound('r-trader-2', NOW_S - 10, null)) : snap(null)),
    )
    await renderHero()
    expect(screen.getByText('#r-trader-2')).toBeInTheDocument()
    expect(screen.getByText('null（进行中）')).toBeInTheDocument()
    const traderCalls = () => holder.getLiveFor.mock.calls.filter(([a]) => a === 'trader').length
    const base = traderCalls()
    await act(async () => vi.advanceTimersByTimeAsync(3000))
    expect(traderCalls()).toBeGreaterThan(base)
    const mid = traderCalls()
    await act(async () => vi.advanceTimersByTimeAsync(3000))
    expect(traderCalls()).toBeGreaterThan(mid)
  })

  it('review_round_start 到达 → 切换显示复盘轮', async () => {
    const utils = await renderHero()
    expect(screen.getByText('#r-trader-1')).toBeInTheDocument()

    await enterReviewRunning(utils)

    expect(screen.getByText('#r-review-1')).toBeInTheDocument()
    expect(screen.getByText('复盘')).toBeInTheDocument()
    expect(screen.getByText('null（进行中）')).toBeInTheDocument()
    expect(holder.getLiveFor).toHaveBeenCalledWith('review')
    expect(screen.queryByText('上轮决策')).not.toBeInTheDocument()
  })

  it('复盘在跑时 research_round_start → 后到优先切研报', async () => {
    const utils = await renderHero()
    await enterReviewRunning(utils)
    expect(screen.getByText('#r-review-1')).toBeInTheDocument()

    holder.getLiveFor.mockImplementation((agent) => {
      if (agent === 'research') return Promise.resolve(snap(RESEARCH_RUNNING))
      if (agent === 'review') return Promise.resolve(snap(REVIEW_RUNNING))
      return Promise.resolve(snap(null))
    })
    await sendWs(utils, { type: 'research_round_start', data: { round_id: 'r-research-1' } })

    expect(screen.getByText('#r-research-1')).toBeInTheDocument()
    expect(screen.getByText('研报')).toBeInTheDocument()
    expect(screen.queryByText('#r-review-1')).not.toBeInTheDocument()
  })

  it('research_round 结束且复盘仍在跑 → 回切复盘', async () => {
    const utils = await renderHero()
    await enterResearchRunning(utils)
    expect(screen.getByText('#r-research-1')).toBeInTheDocument()

    await sendWs(utils, { type: 'research_round', data: { round_id: 'r-research-1', ok: true } })

    expect(screen.getByText('#r-review-1')).toBeInTheDocument()
    expect(screen.getByText('复盘')).toBeInTheDocument()
    expect(screen.getByText('null（进行中）')).toBeInTheDocument() // 复盘仍在跑
  })

  it('全部结束 → 停留最后结束的研报轮（服务器终态轮保持显示 + getRound 拉审计详情）', async () => {
    const utils = await renderHero()
    await enterResearchRunning(utils)
    expect(screen.getByText('#r-research-1')).toBeInTheDocument()

    // 复盘先结束、研报后结束；结束事件后 live 返回带服务器 ended_at 的终态轮（三端点统一保留终态轮）
    const researchEnded = liveRound('r-research-1', NOW_S - 5, NOW_S - 1)
    holder.getLiveFor.mockImplementation((agent) =>
      Promise.resolve(agent === 'research' ? snap(researchEnded) : snap(null)),
    )
    await sendWs(utils, { type: 'review_round', data: { round_id: 'r-review-1', ok: true } })
    await sendWs(utils, { type: 'research_round', data: { round_id: 'r-research-1', ok: true } })

    // 停留研报轮：服务器终态轮保持显示，结束后 lazy 拉审计详情渲染结论
    expect(screen.getByText('#r-research-1')).toBeInTheDocument()
    expect(screen.getByText('研报')).toBeInTheDocument()
    expect(screen.getByText('上轮研报')).toBeInTheDocument()
    expect(holder.getRound).toHaveBeenCalledWith('r-research-1')
    expect(screen.getAllByText('RAW-CONCLUSION').length).toBeGreaterThan(0)
    expect(screen.queryByText(/暂无决策记录/)).not.toBeInTheDocument()
  })

  it('挂载补漏：复盘/研报都在跑且复盘 started_at 最晚 → 初始显示复盘', async () => {
    holder.getLiveFor.mockImplementation((agent) => {
      if (agent === 'review') return Promise.resolve(snap(liveRound('r-review-9', NOW_S - 60, null)))
      if (agent === 'research') return Promise.resolve(snap(liveRound('r-research-9', NOW_S - 300, null)))
      return Promise.resolve(snap(TRADER_ENDED))
    })

    await renderHero()

    expect(screen.getByText('#r-review-9')).toBeInTheDocument()
    expect(screen.getByText('复盘')).toBeInTheDocument()
    expect(screen.getByText('null（进行中）')).toBeInTheDocument()
  })

  it('僵尸轮（ended_at=null 但 started_at 超 30 分钟）：不入栈、不显示进行中态、停止轮询', async () => {
    // 2 小时前「开始」的轮：进程崩溃残留的 ended_at=NULL 脏数据
    const zombie = liveRound('r-zombie-1', NOW_S - 7200, null)
    holder.getLiveFor.mockImplementation((agent) =>
      Promise.resolve(agent === 'trader' ? snap(zombie) : snap(null)),
    )

    await renderHero()

    // 补漏按僵尸过滤不入栈；hero 展示该轮但按非进行中处理（上轮徽标、ended_at 直出 null）
    expect(screen.getByText('#r-zombie-1')).toBeInTheDocument()
    expect(screen.getByText('上轮决策')).toBeInTheDocument()
    expect(screen.queryByText('null（进行中）')).not.toBeInTheDocument()
    expect(holder.getRound).not.toHaveBeenCalled() // ended_at=null → 不拉审计详情
    // 非进行中 → 不启动 3 秒轮询
    const calls = holder.getLiveFor.mock.calls.length
    await act(async () => vi.advanceTimersByTimeAsync(9000))
    expect(holder.getLiveFor.mock.calls.length).toBe(calls)
  })

  it('研报轮结束 → live 返回服务器终态轮：停留显示该轮、拉详情、停止轮询', async () => {
    const utils = await renderHero()
    holder.getLiveFor.mockImplementation((agent) =>
      Promise.resolve(agent === 'research' ? snap(RESEARCH_RUNNING) : snap(null)),
    )
    await sendWs(utils, { type: 'research_round_start', data: { round_id: 'r-research-1' } })
    expect(screen.getByText('#r-research-1')).toBeInTheDocument()

    // 结束事件后 live 返回带服务器 ended_at 的终态轮（三端点统一保留终态轮，round 不变 null）
    const researchEnded = liveRound('r-research-1', NOW_S - 5, NOW_S - 1)
    holder.getLiveFor.mockImplementation((agent) =>
      Promise.resolve(agent === 'research' ? snap(researchEnded) : snap(null)),
    )
    await sendWs(utils, { type: 'research_round', data: { round_id: 'r-research-1', ok: true } })

    // 停留显示终态轮 + lazy 拉审计详情；ended_at 非 null → 非进行中 → 不再 3 秒轮询
    expect(screen.getByText('#r-research-1')).toBeInTheDocument()
    expect(screen.getByText('上轮研报')).toBeInTheDocument()
    expect(screen.queryByText(/暂无决策记录/)).not.toBeInTheDocument()
    expect(holder.getRound).toHaveBeenCalledWith('r-research-1')
    const calls = holder.getLiveFor.mock.calls.length
    await act(async () => vi.advanceTimersByTimeAsync(9000))
    expect(holder.getLiveFor.mock.calls.length).toBe(calls)
  })

  it('WS 断线重连（connected false→true）重新补漏：找回断线期间丢失的 start', async () => {
    const utils = await renderHero() // 首次补漏：无进行中轮，停留 trader 上轮
    expect(screen.getByText('#r-trader-1')).toBeInTheDocument()

    // 断线期间复盘开跑（review_round_start 事件随断线丢失）
    await setConnected(utils, false)
    holder.getLiveFor.mockImplementation((agent) => {
      if (agent === 'review') return Promise.resolve(snap(REVIEW_RUNNING))
      if (agent === 'trader') return Promise.resolve(snap(TRADER_ENDED))
      return Promise.resolve(snap(null))
    })

    // 重连 → 重新补漏发现复盘进行中 → 切换显示复盘轮
    await setConnected(utils, true)
    expect(screen.getByText('#r-review-1')).toBeInTheDocument()
    expect(screen.getByText('null（进行中）')).toBeInTheDocument()
  })

  it('补漏在途时 WS start 先行入栈 → 合并后两者都在栈（WS 与补漏信息都不丢）', async () => {
    // research live 响应挂起（补漏在途）；放行后返回进行中研报轮
    let releaseResearch: (() => void) | null = null
    holder.getLiveFor.mockImplementation((agent) => {
      if (agent === 'research' && releaseResearch === null) {
        return new Promise((resolve) => {
          releaseResearch = () => resolve(snap(RESEARCH_RUNNING))
        })
      }
      if (agent === 'research') return Promise.resolve(snap(RESEARCH_RUNNING))
      if (agent === 'review') return Promise.resolve(snap(REVIEW_RUNNING))
      return Promise.resolve(snap(null))
    })
    const utils = await renderHero() // 挂载补漏已发出，research 响应仍挂起

    // 补漏在途时 WS 先行：review 入栈并显示（旧实现在补漏到达时因栈非空整体跳过，research 会丢失）
    await sendWs(utils, { type: 'review_round_start', data: { round_id: 'r-review-1' } })
    expect(screen.getByText('#r-review-1')).toBeInTheDocument()

    // 补漏到达：research 也进行中 → 合并去重后按 started_at 升序重排；
    // research started_at 更晚（服务器口径的最后开始者）→ current 切 research，review 保留在栈
    releaseResearch!()
    await flush()
    expect(screen.getByText('#r-research-1')).toBeInTheDocument()

    // research 结束 → 回切 review（证明 WS 先行入栈的 review 未被补漏覆盖，双向信息都未丢）
    await sendWs(utils, { type: 'research_round', data: { round_id: 'r-research-1', ok: true } })
    expect(screen.getByText('#r-review-1')).toBeInTheDocument()
    expect(screen.getByText('null（进行中）')).toBeInTheDocument()
  })
})
