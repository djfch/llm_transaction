import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { OpenOrder } from '../api/types'
import OpenOrdersPanel from '../components/console/OpenOrdersPanel'

const holder = vi.hoisted(() => {
  class PanelApiError extends Error {
    constructor(
      public readonly status: number,
      public readonly detail: string,
    ) {
      super(detail)
    }
  }
  return { cancelOpenOrder: vi.fn(), PanelApiError }
})

vi.mock('../api', () => ({
  api: { cancelOpenOrder: (...args: unknown[]) => holder.cancelOpenOrder(...args) },
  ApiError: holder.PanelApiError,
}))

const longOrder: OpenOrder = {
  id: 'order-long',
  contract: 'BTC_USDT',
  size: 12,
  left: 12,
  price: 65_000,
  tif: 'gtc',
  reduce_only: false,
  status: 'open',
  stop_loss_price: 62_000,
  take_profit_price: 70_000,
}

const shortOrder: OpenOrder = {
  ...longOrder,
  id: 'order-short',
  contract: 'ETH_USDT',
  size: -8,
  left: 5,
  price: 1_900,
  reduce_only: true,
  stop_loss_price: null,
  take_profit_price: null,
}

beforeEach(() => {
  holder.cancelOpenOrder.mockReset()
})

describe('OpenOrdersPanel(未成交挂单)', () => {
  it('空持仓时仍显示挂单区域和空状态', () => {
    render(<OpenOrdersPanel orders={[]} />)
    expect(screen.getByText('未成交挂单')).toBeInTheDocument()
    expect(screen.getByText('当前无未成交挂单')).toBeInTheDocument()
  })

  it('渲染多空方向与指定委托字段', () => {
    render(<OpenOrdersPanel orders={[longOrder, shortOrder]} />)
    expect(screen.getByText('多')).toBeInTheDocument()
    expect(screen.getByText('空')).toBeInTheDocument()
    expect(screen.getAllByText('委托张数')).toHaveLength(2)
    expect(screen.getAllByText('未成交张数')).toHaveLength(2)
    expect(screen.getAllByText('委托价')).toHaveLength(2)
    expect(screen.getAllByText('有效方式')).toHaveLength(2)
    expect(screen.getAllByText('只减仓')).toHaveLength(2)
    expect(screen.getByText('止损价')).toBeInTheDocument()
    expect(screen.getByText('止盈价')).toBeInTheDocument()
    expect(screen.getByText('62,000.00')).toBeInTheDocument()
    expect(screen.getByText('70,000.00')).toBeInTheDocument()
    expect(screen.queryByText(/size\(|left\(|price\(|tif\(|reduce_only\(/)).not.toBeInTheDocument()
    expect(screen.getByText('是')).toBeInTheDocument()
  })

  it('未配置保护价时不显示止盈止损字段', () => {
    render(
      <OpenOrdersPanel
        orders={[{ ...shortOrder, stop_loss_price: null, take_profit_price: null }]}
      />,
    )
    expect(screen.queryByText('止损价')).not.toBeInTheDocument()
    expect(screen.queryByText('止盈价')).not.toBeInTheDocument()
  })

  it('第一次点击只进入确认态，第二次成功后隐藏卡片并通知父级', async () => {
    const onChanged = vi.fn()
    holder.cancelOpenOrder.mockResolvedValue({
      id: longOrder.id,
      contract: longOrder.contract,
      status: 'finished',
      finish_as: 'cancelled',
      warning: '',
    })
    render(<OpenOrdersPanel orders={[longOrder]} onChanged={onChanged} />)

    fireEvent.click(screen.getByRole('button', { name: '手动撤单' }))
    expect(holder.cancelOpenOrder).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: '再次点击确认撤单' })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '再次点击确认撤单' }))
    await waitFor(() => expect(holder.cancelOpenOrder).toHaveBeenCalledWith('BTC_USDT', 'order-long'))
    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1))
    expect(screen.queryByText('BTC_USDT')).not.toBeInTheDocument()
    expect(screen.getByText(/已撤销挂单/)).toBeInTheDocument()
  })

  it('超过 3 秒后确认态自动失效，不发起撤单', () => {
    vi.useFakeTimers()
    try {
      render(<OpenOrdersPanel orders={[longOrder]} />)

      fireEvent.click(screen.getByRole('button', { name: '手动撤单' }))
      act(() => vi.advanceTimersByTime(3000))

      expect(screen.getByRole('button', { name: '手动撤单' })).toBeInTheDocument()
      expect(holder.cancelOpenOrder).not.toHaveBeenCalled()
    } finally {
      vi.useRealTimers()
    }
  })

  it('请求中禁用撤单按钮并阻止重复请求', async () => {
    let resolve!: (value: unknown) => void
    holder.cancelOpenOrder.mockImplementation(
      () =>
        new Promise((done) => {
          resolve = done
        }),
    )
    render(<OpenOrdersPanel orders={[longOrder]} />)

    fireEvent.click(screen.getByRole('button', { name: '手动撤单' }))
    fireEvent.click(screen.getByRole('button', { name: '再次点击确认撤单' }))

    const pendingButton = screen.getByRole('button', { name: '撤单中…' })
    expect(pendingButton).toBeDisabled()
    fireEvent.click(pendingButton)
    expect(holder.cancelOpenOrder).toHaveBeenCalledTimes(1)

    resolve({
      id: longOrder.id,
      contract: longOrder.contract,
      status: 'finished',
      finish_as: 'cancelled',
      warning: '',
    })
    await waitFor(() => expect(screen.queryByText('BTC_USDT')).not.toBeInTheDocument())
  })

  it('订单已终态时移除旧卡片并触发刷新', async () => {
    const onChanged = vi.fn()
    holder.cancelOpenOrder.mockRejectedValue(
      new holder.PanelApiError(409, '挂单已成交，已刷新'),
    )
    render(<OpenOrdersPanel orders={[longOrder]} onChanged={onChanged} />)

    fireEvent.click(screen.getByRole('button', { name: '手动撤单' }))
    fireEvent.click(screen.getByRole('button', { name: '再次点击确认撤单' }))

    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1))
    expect(screen.queryByText('BTC_USDT')).not.toBeInTheDocument()
    expect(screen.getByText('挂单已成交，已刷新')).toBeInTheDocument()
  })

  it('撤单失败保留卡片并显示错误原因', async () => {
    holder.cancelOpenOrder.mockRejectedValue(new Error('网关暂时不可用'))
    render(<OpenOrdersPanel orders={[shortOrder]} />)

    fireEvent.click(screen.getByRole('button', { name: '手动撤单' }))
    fireEvent.click(screen.getByRole('button', { name: '再次点击确认撤单' }))

    expect(await screen.findByText('网关暂时不可用')).toBeInTheDocument()
    expect(screen.getByText('ETH_USDT')).toBeInTheDocument()
  })
})
