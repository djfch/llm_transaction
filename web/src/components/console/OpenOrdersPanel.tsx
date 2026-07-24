import { useEffect, useRef, useState } from 'react'
import { api, ApiError } from '../../api'
import type { OpenOrder } from '../../api/types'
import { fmtNum, fmtPrice, fmtSigned } from '../../utils/format'

const CONFIRM_WINDOW_MS = 3000

/** 渲染单个挂单字段的标签和值，保持所有卡片信息对齐。 */
function Field({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between gap-3">
      <span className="text-zinc-500">{label}</span>
      <span className="font-mono tabular-nums text-zinc-200">{value}</span>
    </div>
  )
}

/** 管理撤单二次确认、请求状态与失败提示，成功时交由父级刷新数据。 */
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

  // 首击只进入三秒确认态；确认态内再次点击才请求后端撤单。
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
      onCancelled(result.warning || `已撤销挂单 ${order.id}`)
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        onCancelled(error.detail || '挂单已不处于 open 状态，已刷新')
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

/** 依据带方向的 size 生成多空展示文案和颜色。 */
function direction(order: OpenOrder) {
  if (order.size > 0) {
    return { text: '多 LONG', cls: 'border-emerald-400/40 bg-emerald-400/15 text-emerald-300' }
  }
  if (order.size < 0) {
    return { text: '空 SHORT', cls: 'border-rose-400/40 bg-rose-400/15 text-rose-300' }
  }
  return { text: '未知', cls: 'border-zinc-500/40 bg-zinc-500/15 text-zinc-300' }
}

/** 展示一张未成交挂单卡片，并提供受二次确认保护的撤单按钮。 */
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
        <Field label="委托张数" value={fmtSigned(order.size, 0)} />
        <Field label="未成交张数" value={fmtNum(order.left, 0)} />
        <Field label="委托价" value={fmtPrice(order.price)} />
        <Field label="有效方式" value={order.tif || '-'} />
        <Field
          label="只减仓"
          value={order.reduce_only ? '是' : '否'}
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
          {pending ? '撤单中…' : armed ? '再次点击确认撤单' : '手动撤单'}
        </button>
        {message ? <p className="text-[11px] text-rose-300">{message}</p> : null}
      </div>
    </article>
  )
}

/** 在持仓区域展示未成交挂单；撤单后先本地隐藏，再通知上层刷新账户和列表。 */
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

  // 乐观移除已成功或已终态的卡片，避免刷新请求返回前仍显示旧挂单。
  const handleCancelled = (order: OpenOrder, message: string) => {
    setHiddenIds((ids) => new Set(ids).add(order.id))
    setNotice(message)
    onChanged?.()
  }

  return (
    <section className="space-y-3">
      <div className="flex items-center gap-2">
        <h2 className="text-sm font-semibold text-zinc-300">{'未成交挂单 open_orders'}</h2>
        <span className="rounded-full border border-white/10 bg-white/5 px-2 py-0.5 font-mono text-[10px] tabular-nums text-zinc-400">
          {visibleOrders.length}
        </span>
      </div>
      {notice ? <p className="rounded-lg border border-emerald-400/30 bg-emerald-400/10 px-3 py-2 text-[11px] text-emerald-200">{notice}</p> : null}
      {visibleOrders.length === 0 ? (
        <div className="rounded-xl border border-white/5 bg-zinc-900/60 p-8 text-center text-sm text-zinc-500 backdrop-blur">
          {'当前无未成交挂单'}
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
