/**
 * 持仓卡片测试：手动平仓两段确认流程（mock ApiClient）。
 * 第一段仅进入待确认态不发请求；第二段才真正调用 closePosition。
 *
 * 注意：mock 采用"每个用例新建 vi.fn"（closeImpl 持有器），而不是共享 mock + mockClear。
 * vitest 3.2 对 mockClear/mockReset 之后的 mock 拒绝会误报为未处理 Promise 拒绝（最小复现确认）。
 */
import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { ApiError } from '../api/http'
import type { ClosePositionResult, Position } from '../api/types'
import PositionCard from '../components/PositionCard'

// 当前用例的 closePosition 实现（每个用例重新赋值，天然隔离，无需清理）
const closeImpl = vi.hoisted(() => ({
  fn: vi.fn() as ReturnType<typeof vi.fn<(c: string) => Promise<ClosePositionResult>>>,
}))
vi.mock('../api', async () => ({
  ApiError: (await import('../api/http')).ApiError,
  api: { closePosition: (contract: string) => closeImpl.fn(contract) },
}))

const position: Position = {
  contract: 'BTC_USDT',
  size: 12,
  entry_price: 118_320,
  mark_price: 119_650,
  leverage: 3,
  unrealised_pnl: 159.6,
  liq_price: 82_400,
}

describe('PositionCard(持仓卡片) 手动平仓', () => {
  it('两段确认：第一次点击不发请求，第二次才平仓并回调 onClosed', async () => {
    closeImpl.fn = vi.fn<(c: string) => Promise<ClosePositionResult>>().mockResolvedValue({
      contract: 'BTC_USDT',
      status: 'closed',
      fill_price: 119_650,
      text: '已按标记价 119650 市价平仓',
    })
    const onClosed = vi.fn()
    render(<PositionCard position={position} onClosed={onClosed} />)

    // 第一次点击：进入待确认态，尚未请求
    fireEvent.click(screen.getByRole('button', { name: '手动平仓' }))
    expect(await screen.findByRole('button', { name: '再次点击确认平仓' })).toBeInTheDocument()
    expect(closeImpl.fn).not.toHaveBeenCalled()

    // 第二次点击：真正平仓 → 结果文本 + onClosed 回调
    fireEvent.click(screen.getByRole('button', { name: '再次点击确认平仓' }))
    expect(await screen.findByText('已按标记价 119650 市价平仓')).toBeInTheDocument()
    expect(closeImpl.fn).toHaveBeenCalledTimes(1)
    expect(closeImpl.fn).toHaveBeenCalledWith('BTC_USDT')
    expect(onClosed).toHaveBeenCalledTimes(1)
  })

  it('422 风控拒绝：展示后端返回的风控原因', async () => {
    closeImpl.fn = vi
      .fn<(c: string) => Promise<ClosePositionResult>>()
      .mockRejectedValue(new ApiError(422, '单仓名义价值占权益超过上限'))
    render(<PositionCard position={position} />)

    fireEvent.click(screen.getByRole('button', { name: '手动平仓' }))
    fireEvent.click(await screen.findByRole('button', { name: '再次点击确认平仓' }))

    expect(await screen.findByText('风控拒绝：单仓名义价值占权益超过上限')).toBeInTheDocument()
    expect(closeImpl.fn).toHaveBeenCalledTimes(1)
  })
})
