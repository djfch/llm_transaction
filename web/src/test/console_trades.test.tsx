/**
 * 成交记录面板（console）测试：round 徽标渲染（#短号 / 空归属灰「-」）、
 * 行点击触发 RoundFocus 定位（空归属行不触发）、watchlist 驱动的合约筛选传参、
 * WS round 事件 → 失效重拉当前页（回归 M2）。
 * ApiClient 全量 mock（不依赖 mock.ts 内存态）；WS 经 wsHolder.lastMessage 可控派发。
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { Trade, TradesPageResult, Watchlist, WsMessage } from '../api/types'
import TradesTable from '../components/console/TradesTable'
import { RoundFocusProvider, useRoundFocus } from '../hooks/useRoundFocus'

// 固定夹具：3 笔，t1/t3 有归属（验证短号取前 8 位），t2 无归属（round_id 空串）
const TRADES: Trade[] = [
  {
    id: 1,
    round_id: 'a1b2c3d4000111aa111111111111aaaa',
    time: '2024-01-01T10:00:00.000Z',
    contract: 'BTC_USDT',
    size: 4,
    price: 115_000,
    fee: 2.3,
    pnl: 18.5,
    source: 'llm_open',
  },
  {
    id: 2,
    round_id: '',
    time: '2024-01-01T09:00:00.000Z',
    contract: 'ETH_USDT',
    size: -10,
    price: 3_350,
    fee: 0.8,
    pnl: -6,
    source: 'liquidation',
  },
  {
    id: 3,
    round_id: 'e5f6a7b8000311aa111111111111aaaa',
    time: '2024-01-01T08:00:00.000Z',
    contract: 'BTC_USDT',
    size: -4,
    price: 115_100,
    fee: 2.3,
    pnl: 12,
    source: 'tpsl_close',
  },
]

// mock ../api：getTrades 按 offset/limit/contract 做服务端分页与筛选；getWatchlist 驱动筛选项
const holder = vi.hoisted(() => ({
  getTrades: vi.fn() as ReturnType<
    typeof vi.fn<(o: number, l: number, c?: string) => Promise<TradesPageResult>>
  >,
  getWatchlist: vi.fn() as ReturnType<typeof vi.fn<() => Promise<Watchlist>>>,
}))
vi.mock('../api', () => ({
  api: {
    getTrades: (o: number, l: number, c?: string) => holder.getTrades(o, l, c),
    getWatchlist: () => holder.getWatchlist(),
  },
}))
// WS 可控桩：测试改写 wsHolder.lastMessage 后 rerender 即可派发消息
const wsHolder = vi.hoisted(() => ({ lastMessage: null as WsMessage | null }))
vi.mock('../hooks/useWs', () => ({
  useWs: () => ({ connected: true, lastMessage: wsHolder.lastMessage }),
}))

beforeEach(() => {
  wsHolder.lastMessage = null
  holder.getTrades = vi
    .fn<(o: number, l: number, c?: string) => Promise<TradesPageResult>>()
    .mockImplementation((offset, limit, contract) => {
      const list = TRADES.filter((t) => !contract || t.contract === contract)
      return Promise.resolve({ items: list.slice(offset, offset + limit), total: list.length, offset, limit })
    })
  holder.getWatchlist = vi
    .fn<() => Promise<Watchlist>>()
    .mockResolvedValue({ settle: 'usdt', contracts: ['BTC_USDT', 'ETH_USDT'] })
})

/** 定位探针：实时显示 RoundFocus 目标 roundId */
function FocusProbe() {
  const { target } = useRoundFocus()
  return <div data-testid="focus-target">{target?.roundId ?? ''}</div>
}

/** 表格 JSX（rerender 派发 WS 消息时需同一结构） */
function tableUi() {
  return (
    <RoundFocusProvider>
      <FocusProbe />
      <TradesTable />
    </RoundFocusProvider>
  )
}

function renderTable() {
  return render(tableUi())
}

describe('TradesTable(成交记录)', () => {
  it('round 徽标：有归属显示 #短号(前8位)，无归属灰显 -', async () => {
    renderTable()

    expect(await screen.findByText('#a1b2c3d4')).toBeInTheDocument()
    expect(screen.getByText('#e5f6a7b8')).toBeInTheDocument()
    expect(screen.getByText('LLM开仓')).toBeInTheDocument()
    expect(screen.getByText('止盈止损')).toBeInTheDocument()
    // 无归属行（强平）：来源徽标旁为「-」，无 # 徽标
    const cell = screen.getByText('强平').closest('td')
    expect(cell?.textContent).toContain('-')
    expect(cell?.textContent).not.toContain('#')
  })

  it('点击有归属行 → 触发 focus 定位；无归属行不触发', async () => {
    renderTable()
    const probe = screen.getByTestId('focus-target')

    fireEvent.click(await screen.findByText('#a1b2c3d4'))
    expect(probe.textContent).toBe('a1b2c3d4000111aa111111111111aaaa')

    // 无归属行点击不覆盖当前定位
    fireEvent.click(screen.getByText('强平'))
    expect(probe.textContent).toBe('a1b2c3d4000111aa111111111111aaaa')
  })

  it('合约筛选下拉由 watchlist 驱动，切换带 contract 重新请求并回到第一页', async () => {
    renderTable()

    const select = await screen.findByLabelText(/合约筛选/)
    // 选项来自 watchlist（不硬编码）
    expect(holder.getWatchlist).toHaveBeenCalled()
    expect(screen.getByRole('option', { name: 'ETH_USDT' })).toBeInTheDocument()

    fireEvent.change(select, { target: { value: 'ETH_USDT' } })
    expect(await screen.findByText('第 1/1 页 · 共 1 笔')).toBeInTheDocument()
    expect(holder.getTrades).toHaveBeenLastCalledWith(0, 20, 'ETH_USDT')
  })

  it('未知动态成交来源保留原始值', async () => {
    holder.getTrades.mockResolvedValue({
      items: [{ ...TRADES[0], id: 99, source: 'external_fill' }],
      total: 1,
      offset: 0,
      limit: 20,
    })
    renderTable()

    expect(await screen.findByText('external_fill')).toBeInTheDocument()
    expect(screen.queryByText('llm_open')).not.toBeInTheDocument()
  })

  it('WS round 事件 → 失效重拉当前页（回归 M2：新轮成交需及时上表）', async () => {
    const { rerender } = renderTable()
    await screen.findByText('#a1b2c3d4')
    expect(holder.getTrades).toHaveBeenCalledTimes(1)

    // 后端广播轮结束：仅作失效信号，重拉当前页（保持 offset/筛选口径）
    wsHolder.lastMessage = {
      type: 'round',
      data: { round_id: 'r-new', ok: true, wake_source: '价格触发' },
    }
    rerender(tableUi())

    await waitFor(() => expect(holder.getTrades).toHaveBeenCalledTimes(2))
    expect(holder.getTrades).toHaveBeenLastCalledWith(0, 20, undefined)
  })
})
