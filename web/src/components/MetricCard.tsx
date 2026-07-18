import type { ReactNode } from 'react'

/** 指标卡片：标签使用 `变量名(含义)` 格式，如 equity(账户权益) */
export default function MetricCard({
  label,
  value,
  tone = 'default',
}: {
  label: string
  value: ReactNode
  tone?: 'default' | 'up' | 'down' | 'warn'
}) {
  const toneClass = {
    default: 'text-slate-100',
    up: 'text-emerald-400',
    down: 'text-rose-400',
    warn: 'text-amber-400',
  }[tone]

  return (
    <div className="rounded-xl border border-slate-800 bg-slate-900/70 p-4">
      <div className="text-xs text-slate-500">{label}</div>
      <div className={`mt-2 text-xl font-semibold tabular-nums ${toneClass}`}>{value}</div>
    </div>
  )
}
