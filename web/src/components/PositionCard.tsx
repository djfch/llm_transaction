import { useEffect, useRef, useState } from 'react'
import { api, ApiError } from '../api'
import type { Position } from '../api/types'
import { fmtNum, fmtPrice, fmtSigned, pnlClass } from '../utils/format'
import Card from './Card'

/** 持仓卡片：单合约全字段展示（标签遵循 `变量名(含义)` 格式），带手动平仓（两段确认） */
export default function PositionCard({
  position,
  onClosed,
}: {
  position: Position
  /** 平仓成功后的回调（父级据此刷新持仓/账户） */
  onClosed?: () => void
}) {
  const { contract, size } = position
  const direction = size > 0 ? '多' : size < 0 ? '空' : '平'
  const dirClass =
    size > 0
      ? 'bg-emerald-500/15 text-emerald-400'
      : size < 0
        ? 'bg-rose-500/15 text-rose-400'
        : 'bg-slate-700/40 text-slate-400'

  // 手动平仓：armed=待确认（3 秒有效），pending=请求中，message=结果或风控原因
  const [armed, setArmed] = useState(false)
  const [pending, setPending] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)

  // 卸载时清理待确认计时器
  useEffect(
    () => () => {
      if (timer.current) clearTimeout(timer.current)
    },
    [],
  )

  const handleClose = async () => {
    if (pending) return
    if (!armed) {
      setArmed(true)
      timer.current = setTimeout(() => setArmed(false), 3000)
      return
    }
    if (timer.current) clearTimeout(timer.current)
    setArmed(false)
    setPending(true)
    setMessage(null)
    try {
      const res = await api.closePosition(contract)
      setMessage(res.text || `已平仓，fill_price(成交均价) ${res.fill_price}`)
      onClosed?.()
    } catch (e) {
      // 422 为风控拒绝，展示后端给出的原因；其他 ApiError 同样展示 detail
      setMessage(
        e instanceof ApiError ? (e.status === 422 ? `风控拒绝：${e.detail}` : e.detail) : String(e),
      )
    } finally {
      setPending(false)
    }
  }

  const fields: Array<[string, string]> = [
    ['size(持仓张数)', String(size)],
    ['entry_price(开仓均价)', fmtPrice(position.entry_price)],
    ['mark_price(标记价格)', fmtPrice(position.mark_price)],
    ['leverage(杠杆倍数)', `${position.leverage}x`],
    ['liq_price(强平价格)', fmtPrice(position.liq_price)],
  ]

  return (
    <Card>
      <div className="mb-3 flex items-center justify-between">
        <span className="font-semibold text-slate-100">{contract}</span>
        <span className={`rounded px-2 py-0.5 text-xs font-medium ${dirClass}`}>{direction}</span>
      </div>
      <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
        {fields.map(([label, value]) => (
          <div key={label}>
            <dt className="text-xs text-slate-500">{label}</dt>
            <dd className="mt-0.5 tabular-nums text-slate-200">{value}</dd>
          </div>
        ))}
        <div>
          <dt className="text-xs text-slate-500">unrealised_pnl(未实现盈亏)</dt>
          <dd className={`mt-0.5 tabular-nums font-medium ${pnlClass(position.unrealised_pnl)}`}>
            {fmtSigned(position.unrealised_pnl)}
          </dd>
        </div>
        <div>
          <dt className="text-xs text-slate-500">notional(名义价值)</dt>
          <dd className="mt-0.5 tabular-nums text-slate-200">
            {fmtNum(Math.abs(size) * position.mark_price, 0)}
          </dd>
        </div>
      </dl>
      <div className="mt-4 border-t border-slate-800 pt-3">
        <button
          type="button"
          disabled={pending}
          onClick={handleClose}
          className={`rounded-lg px-3 py-1.5 text-xs font-medium transition-colors disabled:opacity-50 ${
            armed
              ? 'bg-amber-500 text-slate-950 hover:bg-amber-400'
              : 'bg-rose-600/80 text-white hover:bg-rose-500'
          }`}
        >
          {pending ? '平仓中…' : armed ? '再次点击确认平仓' : '手动平仓'}
        </button>
        {message && <p className="mt-2 text-xs text-slate-400">{message}</p>}
      </div>
    </Card>
  )
}
