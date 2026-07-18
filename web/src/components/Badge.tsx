/** 状态徽标：已配置/未配置、风控判定等场景通用 */
export default function Badge({
  text,
  tone = 'neutral',
}: {
  text: string
  tone?: 'ok' | 'danger' | 'warn' | 'neutral' | 'info'
}) {
  const toneClass = {
    ok: 'bg-emerald-500/15 text-emerald-400 border-emerald-500/30',
    danger: 'bg-rose-500/15 text-rose-400 border-rose-500/30',
    warn: 'bg-amber-500/15 text-amber-400 border-amber-500/30',
    neutral: 'bg-slate-700/40 text-slate-400 border-slate-600/40',
    info: 'bg-sky-500/15 text-sky-400 border-sky-500/30',
  }[tone]

  return (
    <span className={`inline-block rounded border px-2 py-0.5 text-xs font-medium ${toneClass}`}>
      {text}
    </span>
  )
}
