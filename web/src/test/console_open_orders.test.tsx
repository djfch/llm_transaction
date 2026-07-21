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
}

const shortOrder: OpenOrder = {
  ...longOrder,
  id: 'order-short',
  contract: 'ETH_USDT',
  size: -8,
  left: 5,
  price: 1_900,
  reduce_only: true,
}

beforeEach(() => {
  holder.cancelOpenOrder.mockReset()
})

describe('OpenOrdersPanel(\u672a\u6210\u4ea4\u6302\u5355)', () => {
  it('\u7a7a\u6301\u4ed3\u65f6\u4ecd\u663e\u793a\u6302\u5355\u533a\u57df\u548c\u7a7a\u72b6\u6001', () => {
    render(<OpenOrdersPanel orders={[]} />)
    expect(screen.getByText('\u672a\u6210\u4ea4\u6302\u5355 open_orders')).toBeInTheDocument()
    expect(screen.getByText('\u5f53\u524d\u65e0\u672a\u6210\u4ea4\u6302\u5355')).toBeInTheDocument()
  })

  it('\u6e32\u67d3\u591a\u7a7a\u65b9\u5411\u4e0e\u6307\u5b9a\u59d4\u6258\u5b57\u6bb5', () => {
    render(<OpenOrdersPanel orders={[longOrder, shortOrder]} />)
    expect(screen.getByText('\u591a LONG')).toBeInTheDocument()
    expect(screen.getByText('\u7a7a SHORT')).toBeInTheDocument()
    expect(screen.getAllByText('size(\u59d4\u6258\u5f20\u6570)')).toHaveLength(2)
    expect(screen.getAllByText('left(\u672a\u6210\u4ea4\u5f20\u6570)')).toHaveLength(2)
    expect(screen.getAllByText('price(\u59d4\u6258\u4ef7)')).toHaveLength(2)
    expect(screen.getAllByText('tif(\u6709\u6548\u65b9\u5f0f)')).toHaveLength(2)
    expect(screen.getAllByText('reduce_only(\u53ea\u51cf\u4ed3)')).toHaveLength(2)
    expect(screen.getByText('\u662f')).toBeInTheDocument()
  })

  it('\u7b2c\u4e00\u6b21\u70b9\u51fb\u53ea\u8fdb\u5165\u786e\u8ba4\u6001\uff0c\u7b2c\u4e8c\u6b21\u6210\u529f\u540e\u9690\u85cf\u5361\u7247\u5e76\u901a\u77e5\u7236\u7ea7', async () => {
    const onChanged = vi.fn()
    holder.cancelOpenOrder.mockResolvedValue({
      id: longOrder.id,
      contract: longOrder.contract,
      status: 'finished',
      finish_as: 'cancelled',
      warning: '',
    })
    render(<OpenOrdersPanel orders={[longOrder]} onChanged={onChanged} />)

    fireEvent.click(screen.getByRole('button', { name: '\u624b\u52a8\u64a4\u5355' }))
    expect(holder.cancelOpenOrder).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: '\u518d\u6b21\u70b9\u51fb\u786e\u8ba4\u64a4\u5355' })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '\u518d\u6b21\u70b9\u51fb\u786e\u8ba4\u64a4\u5355' }))
    await waitFor(() => expect(holder.cancelOpenOrder).toHaveBeenCalledWith('BTC_USDT', 'order-long'))
    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1))
    expect(screen.queryByText('BTC_USDT')).not.toBeInTheDocument()
    expect(screen.getByText(/\u5df2\u64a4\u9500\u6302\u5355/)).toBeInTheDocument()
  })

  it('\u8d85\u8fc7 3 \u79d2\u540e\u786e\u8ba4\u6001\u81ea\u52a8\u5931\u6548\uff0c\u4e0d\u53d1\u8d77\u64a4\u5355', () => {
    vi.useFakeTimers()
    try {
      render(<OpenOrdersPanel orders={[longOrder]} />)

      fireEvent.click(screen.getByRole('button', { name: '\u624b\u52a8\u64a4\u5355' }))
      act(() => vi.advanceTimersByTime(3000))

      expect(screen.getByRole('button', { name: '\u624b\u52a8\u64a4\u5355' })).toBeInTheDocument()
      expect(holder.cancelOpenOrder).not.toHaveBeenCalled()
    } finally {
      vi.useRealTimers()
    }
  })

  it('\u8bf7\u6c42\u4e2d\u7981\u7528\u64a4\u5355\u6309\u94ae\u5e76\u963b\u6b62\u91cd\u590d\u8bf7\u6c42', async () => {
    let resolve!: (value: unknown) => void
    holder.cancelOpenOrder.mockImplementation(
      () =>
        new Promise((done) => {
          resolve = done
        }),
    )
    render(<OpenOrdersPanel orders={[longOrder]} />)

    fireEvent.click(screen.getByRole('button', { name: '\u624b\u52a8\u64a4\u5355' }))
    fireEvent.click(screen.getByRole('button', { name: '\u518d\u6b21\u70b9\u51fb\u786e\u8ba4\u64a4\u5355' }))

    const pendingButton = screen.getByRole('button', { name: '\u64a4\u5355\u4e2d\u2026' })
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

  it('\u8ba2\u5355\u5df2\u7ec8\u6001\u65f6\u79fb\u9664\u65e7\u5361\u7247\u5e76\u89e6\u53d1\u5237\u65b0', async () => {
    const onChanged = vi.fn()
    holder.cancelOpenOrder.mockRejectedValue(
      new holder.PanelApiError(409, '\u6302\u5355\u5df2\u6210\u4ea4\uff0c\u5df2\u5237\u65b0'),
    )
    render(<OpenOrdersPanel orders={[longOrder]} onChanged={onChanged} />)

    fireEvent.click(screen.getByRole('button', { name: '\u624b\u52a8\u64a4\u5355' }))
    fireEvent.click(screen.getByRole('button', { name: '\u518d\u6b21\u70b9\u51fb\u786e\u8ba4\u64a4\u5355' }))

    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1))
    expect(screen.queryByText('BTC_USDT')).not.toBeInTheDocument()
    expect(screen.getByText('\u6302\u5355\u5df2\u6210\u4ea4\uff0c\u5df2\u5237\u65b0')).toBeInTheDocument()
  })

  it('\u64a4\u5355\u5931\u8d25\u4fdd\u7559\u5361\u7247\u5e76\u663e\u793a\u9519\u8bef\u539f\u56e0', async () => {
    holder.cancelOpenOrder.mockRejectedValue(new Error('\u7f51\u5173\u6682\u65f6\u4e0d\u53ef\u7528'))
    render(<OpenOrdersPanel orders={[shortOrder]} />)

    fireEvent.click(screen.getByRole('button', { name: '\u624b\u52a8\u64a4\u5355' }))
    fireEvent.click(screen.getByRole('button', { name: '\u518d\u6b21\u70b9\u51fb\u786e\u8ba4\u64a4\u5355' }))

    expect(await screen.findByText('\u7f51\u5173\u6682\u65f6\u4e0d\u53ef\u7528')).toBeInTheDocument()
    expect(screen.getByText('ETH_USDT')).toBeInTheDocument()
  })
})
