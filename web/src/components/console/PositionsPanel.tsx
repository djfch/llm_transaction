/**
 * 持仓卡片列表：多绿空红左边条 + 方向徽标 + 杠杆 + 全字段，
 * 「手动平仓」采用两段确认，并展示 ApiError 422(风控拒绝)/409 错误。
 * 空持仓显示空态。数据由父级装配层下发（哑组件）。
 */
import { useEffect, useRef, useState } from 'react'
import { api, ApiError } from '../../api'
import type { Position } from '../../api/types'
import { fmtNum, fmtPct2, fmtPrice, fmtSigned, fmtSignedPct, pnlClass } from '../../utils/format'

/** 强平缓冲（0-1 比例）：|mark − liq| / mark；mark/liq 无效（非有限数或 ≤0）时 NaN → 显示 '-' */
function liqBufferRatio(p: Position): number {
  if (!Number.isFinite(p.mark_price) || !Number.isFinite(p.liq_price)) return NaN
  if (p.mark_price <= 0 || p.liq_price <= 0) return NaN
  return Math.abs(p.mark_price - p.liq_price) / p.mark_price
}

/** 保证金收益率（0-1 比例）：unrealised_pnl / margin；margin ≤0 或无效时 NaN → 显示 '-' */
function marginRoiRatio(p: Position): number {
  if (!Number.isFinite(p.margin) || p.margin <= 0) return NaN
  return p.unrealised_pnl / p.margin
}

/** 手动平仓两段确认状态机：armed=待确认(3秒有效)，pending=请求中，message=结果/错误 */
function useClosePosition(contract: string, onChanged?: () => void) {
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
      setMessage(res.text || `已平仓，成交均价 ${res.fill_price}`)
      onChanged?.()
    } catch (e) {
      // 422 为风控拒绝，409 等非 2xx 展示后端 detail（沿用现有模式）
      setMessage(
        e instanceof ApiError ? (e.status === 422 ? `风控拒绝：${e.detail}` : e.detail) : String(e),
      )
    } finally {
      setPending(false)
    }
  }

  return { armed, pending, message, handleClose }
}

/** 字段行：label(含义) 左、等宽数值右 */
function Field({ label, value, cls }: { label: string; value: string; cls?: string }) {
  return (
    <div className="flex justify-between">
      <span className="text-zinc-500">{label}</span>
      <span className={`font-mono tabular-nums ${cls ?? 'text-zinc-200'}`}>{value}</span>
    </div>
  )
}

/** 单持仓卡片 */
function PositionItem({
  position,
  onChanged,
}: {
  position: Position
  onChanged?: () => void
}) {
  const { contract, size, leverage } = position
  const isLong = size > 0
  const isShort = size < 0
  const { armed, pending, message, handleClose } = useClosePosition(contract, onChanged)

  const dirBadge = isLong
    ? { text: '多 LONG', cls: 'border-emerald-400/40 bg-emerald-400/15 text-emerald-300' }
    : { text: '空 SHORT', cls: 'border-rose-400/40 bg-rose-400/15 text-rose-300' }
  const edgeCls = isLong
    ? 'border-l-emerald-400/70'
    : isShort
      ? 'border-l-rose-400/70'
      : 'border-l-zinc-600/60'

  return (
    <article
      className={`rounded-xl border border-white/5 border-l-2 bg-zinc-900/60 p-4 backdrop-blur ${edgeCls}`}
    >
      <div className="flex items-center gap-2">
        <span className="font-mono font-bold text-zinc-100">{contract}</span>
        <span className={`rounded border px-1.5 py-0.5 text-[10px] font-bold ${dirBadge.cls}`}>
          {dirBadge.text}
        </span>
        <span className="rounded border border-white/10 bg-white/5 px-1.5 py-0.5 font-mono text-[10px] text-zinc-400">
          {leverage}x
        </span>
        <span
          className={`ml-auto font-mono text-sm font-bold tabular-nums ${pnlClass(position.unrealised_pnl)}`}
        >
          {fmtSigned(position.unrealised_pnl)}
        </span>
      </div>
      <div className="mt-3 grid grid-cols-2 gap-x-4 gap-y-1.5 text-[11px]">
        <Field label="张数" value={fmtSigned(size, 0)} />
        <Field label="保证金" value={fmtNum(position.margin)} />
        <Field label="开仓价" value={fmtPrice(position.entry_price)} />
        <Field label="标记价" value={fmtPrice(position.mark_price)} />
        <Field
          label="止损价"
          value={position.stop_loss_price == null ? '未设置' : fmtPrice(position.stop_loss_price)}
        />
        <Field
          label="止盈价"
          value={position.take_profit_price == null ? '未设置' : fmtPrice(position.take_profit_price)}
        />
        <Field label="强平价" value={fmtPrice(position.liq_price)} cls="text-zinc-400" />
        <Field label="强平缓冲" value={fmtPct2(liqBufferRatio(position))} cls="text-emerald-300" />
        <Field
          label="保证金收益率"
          value={fmtSignedPct(marginRoiRatio(position))}
          cls={pnlClass(marginRoiRatio(position))}
        />
      </div>
      <div className="mt-3 flex items-center gap-3 border-t border-white/5 pt-3">
        <button
          type="button"
          disabled={pending}
          onClick={handleClose}
          className={`rounded-md px-2.5 py-1 text-[11px] font-medium transition disabled:opacity-50 ${
            armed
              ? 'bg-amber-500 text-zinc-950 hover:bg-amber-400'
              : 'border border-rose-400/40 text-rose-300 hover:bg-rose-500/15'
          }`}
        >
          {pending ? '平仓中…' : armed ? '再次点击确认平仓' : '手动平仓'}
        </button>
        {message && <p className="text-[11px] text-zinc-400">{message}</p>}
      </div>
    </article>
  )
}

export default function PositionsPanel({
  positions,
  onChanged,
}: {
  positions: Position[]
  /** 平仓成功后的回调（供父级刷新账户/成交） */
  onChanged?: () => void
}) {
  return (
    <section className="space-y-3">
      <div className="flex items-center gap-2">
        <h2 className="text-sm font-semibold text-zinc-300">当前持仓 positions</h2>
        <span className="rounded-full border border-white/10 bg-white/5 px-2 py-0.5 font-mono text-[10px] tabular-nums text-zinc-400">
          {positions.length}
        </span>
      </div>
      {positions.length === 0 ? (
        <div className="rounded-xl border border-white/5 bg-zinc-900/60 p-8 text-center text-sm text-zinc-500 backdrop-blur">
          当前无持仓
        </div>
      ) : (
        positions.map((p) => <PositionItem key={p.contract} position={p} onChanged={onChanged} />)
      )}
    </section>
  )
}
