import { useEffect, useRef, useState } from 'react'
import { api, ApiError } from '../../api'
import type { OpenOrder } from '../../api/types'
import { fmtNum, fmtPrice, fmtSigned } from '../../utils/format'

const CONFIRM_WINDOW_MS = 3000

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between gap-3">
      <span className="text-zinc-500">{label}</span>
      <span className="font-mono tabular-nums text-zinc-200">{value}</span>
    </div>
  )
}

function useCancelOpenOrder(order: OpenOrder, onCancelled: (message: string) => void) {
  const [armed, setArmed] = useState(false)
  const [pending, setPending] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(
    () => () => {
      if (timer.current) clearTimeout(timer.current)
    },
    [],
  )

  const cancel = async () => {
    if (pending) return
    if (!armed) {
      setArmed(true)
      timer.current = setTimeout(() => setArmed(false), CONFIRM_WINDOW_MS)
      return
    }

    if (timer.current) clearTimeout(timer.current)
    setArmed(false)
    setPending(true)
    setMessage(null)
    try {
      const result = await api.cancelOpenOrder(order.contract, order.id)
      onCancelled(result.warning || `\u5df2\u64a4\u9500\u6302\u5355 ${order.id}`)
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        onCancelled(error.detail || '\u6302\u5355\u5df2\u4e0d\u5904\u4e8e open \u72b6\u6001\uff0c\u5df2\u5237\u65b0')
      } else {
        setMessage(
          error instanceof ApiError ? error.detail : error instanceof Error ? error.message : String(error),
        )
      }
    } finally {
      setPending(false)
    }
  }

  return { armed, pending, message, cancel }
}

function direction(order: OpenOrder) {
  if (order.size > 0) {
    return { text: '\u591a LONG', cls: 'border-emerald-400/40 bg-emerald-400/15 text-emerald-300' }
  }
  if (order.size < 0) {
    return { text: '\u7a7a SHORT', cls: 'border-rose-400/40 bg-rose-400/15 text-rose-300' }
  }
  return { text: '\u672a\u77e5', cls: 'border-zinc-500/40 bg-zinc-500/15 text-zinc-300' }
}

function OpenOrderCard({
  order,
  onCancelled,
}: {
  order: OpenOrder
  onCancelled: (message: string) => void
}) {
  const { armed, pending, message, cancel } = useCancelOpenOrder(order, onCancelled)
  const side = direction(order)

  return (
    <article className="rounded-xl border border-white/5 border-l-2 border-l-amber-400/70 bg-zinc-900/60 p-4 backdrop-blur">
      <div className="flex items-center gap-2">
        <span className="font-mono font-bold text-zinc-100">{order.contract}</span>
        <span className={`rounded border px-1.5 py-0.5 text-[10px] font-bold ${side.cls}`}>{side.text}</span>
        <span className="ml-auto rounded border border-amber-400/30 bg-amber-400/10 px-1.5 py-0.5 text-[10px] text-amber-200">
          open
        </span>
      </div>
      <div className="mt-3 grid grid-cols-2 gap-x-4 gap-y-1.5 text-[11px]">
        <Field label={'size(\u59d4\u6258\u5f20\u6570)'} value={fmtSigned(order.size, 0)} />
        <Field label={'left(\u672a\u6210\u4ea4\u5f20\u6570)'} value={fmtNum(order.left, 0)} />
        <Field label={'price(\u59d4\u6258\u4ef7)'} value={fmtPrice(order.price)} />
        <Field label={'tif(\u6709\u6548\u65b9\u5f0f)'} value={order.tif || '-'} />
        <Field
          label={'reduce_only(\u53ea\u51cf\u4ed3)'}
          value={order.reduce_only ? '\u662f' : '\u5426'}
        />
      </div>
      <div className="mt-3 flex items-center gap-3 border-t border-white/5 pt-3">
        <button
          type="button"
          disabled={pending}
          onClick={cancel}
          className={`rounded-md px-2.5 py-1 text-[11px] font-medium transition disabled:opacity-50 ${
            armed
              ? 'bg-amber-500 text-zinc-950 hover:bg-amber-400'
              : 'border border-rose-400/40 text-rose-300 hover:bg-rose-500/15'
          }`}
        >
          {pending ? '\u64a4\u5355\u4e2d\u2026' : armed ? '\u518d\u6b21\u70b9\u51fb\u786e\u8ba4\u64a4\u5355' : '\u624b\u52a8\u64a4\u5355'}
        </button>
        {message ? <p className="text-[11px] text-rose-300">{message}</p> : null}
      </div>
    </article>
  )
}

export default function OpenOrdersPanel({
  orders,
  onChanged,
}: {
  orders: OpenOrder[]
  onChanged?: () => void
}) {
  const [hiddenIds, setHiddenIds] = useState<Set<string>>(() => new Set())
  const [notice, setNotice] = useState<string | null>(null)
  const visibleOrders = orders.filter((order) => !hiddenIds.has(order.id))

  const handleCancelled = (order: OpenOrder, message: string) => {
    setHiddenIds((ids) => new Set(ids).add(order.id))
    setNotice(message)
    onChanged?.()
  }

  return (
    <section className="space-y-3">
      <div className="flex items-center gap-2">
        <h2 className="text-sm font-semibold text-zinc-300">{'\u672a\u6210\u4ea4\u6302\u5355 open_orders'}</h2>
        <span className="rounded-full border border-white/10 bg-white/5 px-2 py-0.5 font-mono text-[10px] tabular-nums text-zinc-400">
          {visibleOrders.length}
        </span>
      </div>
      {notice ? <p className="rounded-lg border border-emerald-400/30 bg-emerald-400/10 px-3 py-2 text-[11px] text-emerald-200">{notice}</p> : null}
      {visibleOrders.length === 0 ? (
        <div className="rounded-xl border border-white/5 bg-zinc-900/60 p-8 text-center text-sm text-zinc-500 backdrop-blur">
          {'\u5f53\u524d\u65e0\u672a\u6210\u4ea4\u6302\u5355'}
        </div>
      ) : (
        visibleOrders.map((order) => (
          <OpenOrderCard
            key={order.id}
            order={order}
            onCancelled={(message) => handleCancelled(order, message)}
          />
        ))
      )}
    </section>
  )
}
