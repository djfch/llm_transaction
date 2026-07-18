/**
 * 交易记录测试：分页器渲染与翻页交互、服务端筛选传参、source(来源) 徽章映射。
 * ApiClient 全量 mock（不依赖 mock.ts 内存态）。
 */
import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { Trade, TradesPageResult } from '../api/types'
import TradesPage from '../pages/TradesPage'
import { sourceBadge } from '../utils/format'

// 固定 45 笔数据：合约交替、6 种 source 轮转，覆盖徽章全部分支
const SOURCES = ['llm_open', 'llm_close', 'user_close', 'liquidation', 'tpsl_close', '']
const ALL_TRADES: Trade[] = Array.from({ length: 45 }, (_, i) => ({
  id: i + 1,
  time: new Date(1_700_000_000_000 + i * 60_000).toISOString(),
  contract: i % 2 === 0 ? 'BTC_USDT' : 'ETH_USDT',
  size: i % 2 === 0 ? 4 : -4,
  price: 100 + i,
  fee: 0.5,
  pnl: i % 3 === 0 ? 10 : -5,
  source: SOURCES[i % SOURCES.length],
}))

// mock ../api：getTrades 按 offset/limit/contract 做服务端分页与筛选
// 注意：vi.mock 工厂会被提升，getTrades 需用 vi.hoisted 声明
const getTrades = vi.hoisted(() =>
  vi.fn((offset: number, limit: number, contract?: string): Promise<TradesPageResult> => {
    const list = ALL_TRADES.filter((t) => !contract || t.contract === contract)
    return Promise.resolve({ items: list.slice(offset, offset + limit), total: list.length, offset, limit })
  }),
)
vi.mock('../api', () => ({
  api: { getTrades: (...args: unknown[]) => getTrades(...(args as [number, number, string?])) },
}))

beforeEach(() => getTrades.mockClear())

describe('TradesPage(交易记录)', () => {
  it('首屏请求第一页并渲染分页器与 source 徽章', async () => {
    render(<TradesPage />)

    // 分页器：45 笔 / 每页 20 → 3 页
    expect(await screen.findByText('第 1/3 页 · 共 45 笔')).toBeInTheDocument()
    expect(getTrades).toHaveBeenCalledWith(0, 20, undefined)

    // source(来源) 列徽章（首屏 20 笔内覆盖多种来源）
    expect(screen.getAllByText('LLM开仓').length).toBeGreaterThan(0)
    expect(screen.getAllByText('LLM平仓').length).toBeGreaterThan(0)
    expect(screen.getAllByText('用户平仓').length).toBeGreaterThan(0)
    expect(screen.getAllByText('强平').length).toBeGreaterThan(0)
    expect(screen.getAllByText('止盈止损').length).toBeGreaterThan(0)

    // 首屏 20 行
    expect(screen.getAllByRole('row')).toHaveLength(21) // 含表头
    expect(screen.getByRole('button', { name: '上一页' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '下一页' })).toBeEnabled()
  })

  it('翻页交互：下一页/上一页重新请求对应 offset', async () => {
    render(<TradesPage />)
    await screen.findByText('第 1/3 页 · 共 45 笔')

    fireEvent.click(screen.getByRole('button', { name: '下一页' }))
    expect(await screen.findByText('第 2/3 页 · 共 45 笔')).toBeInTheDocument()
    expect(getTrades).toHaveBeenLastCalledWith(20, 20, undefined)

    // 第三页只剩 5 笔，下一页置灰
    fireEvent.click(screen.getByRole('button', { name: '下一页' }))
    expect(await screen.findByText('第 3/3 页 · 共 45 笔')).toBeInTheDocument()
    expect(getTrades).toHaveBeenLastCalledWith(40, 20, undefined)
    expect(screen.getAllByRole('row')).toHaveLength(6)
    expect(screen.getByRole('button', { name: '下一页' })).toBeDisabled()

    fireEvent.click(screen.getByRole('button', { name: '上一页' }))
    expect(await screen.findByText('第 2/3 页 · 共 45 笔')).toBeInTheDocument()
    expect(getTrades).toHaveBeenLastCalledWith(20, 20, undefined)
  })

  it('切换合约筛选：带 contract 重新请求并回到第一页', async () => {
    render(<TradesPage />)
    await screen.findByText('第 1/3 页 · 共 45 笔')

    // 先翻到第二页，再切筛选，验证页码归零
    fireEvent.click(screen.getByRole('button', { name: '下一页' }))
    await screen.findByText('第 2/3 页 · 共 45 笔')

    fireEvent.change(screen.getByLabelText(/contract\(合约筛选\)/), { target: { value: 'BTC_USDT' } })
    // BTC_USDT 有 23 笔 → 2 页
    expect(await screen.findByText('第 1/2 页 · 共 23 笔')).toBeInTheDocument()
    expect(getTrades).toHaveBeenLastCalledWith(0, 20, 'BTC_USDT')
  })
})

describe('sourceBadge(source 徽章映射)', () => {
  it('按契约枚举映射文案与色调', () => {
    expect(sourceBadge('llm_open')).toEqual({ text: 'LLM开仓', tone: 'info' })
    expect(sourceBadge('llm_close')).toEqual({ text: 'LLM平仓', tone: 'warn' })
    expect(sourceBadge('user_close')).toEqual({ text: '用户平仓', tone: 'danger' })
    expect(sourceBadge('liquidation')).toEqual({ text: '强平', tone: 'danger' })
    expect(sourceBadge('tpsl_close')).toEqual({ text: '止盈止损', tone: 'warn' })
    expect(sourceBadge('')).toEqual({ text: '-', tone: 'neutral' })
    expect(sourceBadge('未知来源')).toEqual({ text: '-', tone: 'neutral' })
  })
})
