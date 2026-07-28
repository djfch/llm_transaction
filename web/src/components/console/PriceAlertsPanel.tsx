/**
 * 价格唤醒卡片列表：LLM 经 set_price_alert 设置的未触发预警线（GET /api/alerts）。
 * 样式对齐持仓/挂单方面（琥珀色左边条 = 待触发）；只读展示，内存唯一存储，
 * 触发即从索引移除（重启即失效），下轮刷新即从列表消失。数据由父级装配层下发（哑组件）。
 */
import type { PriceAlert } from '../../api/types'
import { fmtPrice, fmtTime } from '../../utils/format'

/** 字段行：label(含义) 左、等宽数值右（与挂单卡片同一排版） */
function Field({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between gap-3">
      <span className="text-zinc-500">{label}</span>
      <span className="font-mono tabular-nums text-zinc-200">{value}</span>
    </div>
  )
}

/** 方向徽标：above=上破（涨穿触发，绿色）；below=下破（跌穿触发，红色） */
function directionBadge(direction: PriceAlert['direction']) {
  return direction === 'above'
    ? { text: '上破', cls: 'border-emerald-400/40 bg-emerald-400/15 text-emerald-300' }
    : { text: '下破', cls: 'border-rose-400/40 bg-rose-400/15 text-rose-300' }
}

/** 单条价格唤醒卡片 */
function PriceAlertCard({ alert }: { alert: PriceAlert }) {
  const badge = directionBadge(alert.direction)
  return (
    <article className="rounded-xl border border-white/5 border-l-2 border-l-amber-400/70 bg-zinc-900/60 p-4 backdrop-blur">
      <div className="flex items-center gap-2">
        <span className="font-mono font-bold text-zinc-100">{alert.contract}</span>
        <span className={`rounded border px-1.5 py-0.5 text-[10px] font-bold ${badge.cls}`}>
          {badge.text}
        </span>
        <span className="ml-auto rounded border border-amber-400/30 bg-amber-400/10 px-1.5 py-0.5 text-[10px] text-amber-200">
          待触发
        </span>
      </div>
      <div className="mt-3 grid grid-cols-2 gap-x-4 gap-y-1.5 text-[11px]">
        <Field label="触发价" value={fmtPrice(alert.price)} />
        <Field label="设置时间" value={fmtTime(alert.time)} />
      </div>
    </article>
  )
}

export default function PriceAlertsPanel({ alerts }: { alerts: PriceAlert[] }) {
  return (
    <section className="space-y-3">
      <div className="flex items-center gap-2">
        <h2 className="text-sm font-semibold text-zinc-300">价格唤醒</h2>
        <span className="rounded-full border border-white/10 bg-white/5 px-2 py-0.5 font-mono text-[10px] tabular-nums text-zinc-400">
          {alerts.length}
        </span>
      </div>
      {alerts.length === 0 ? (
        <div className="rounded-xl border border-white/5 bg-zinc-900/60 p-8 text-center text-sm text-zinc-500 backdrop-blur">
          当前无价格唤醒
        </div>
      ) : (
        alerts.map((alert) => <PriceAlertCard key={alert.id} alert={alert} />)
      )}
    </section>
  )
}
