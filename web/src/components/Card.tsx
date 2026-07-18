import type { ReactNode } from 'react'

/** 通用卡片容器：深色金融仪表盘风格 */
export default function Card({
  title,
  extra,
  children,
}: {
  title?: string
  extra?: ReactNode
  children: ReactNode
}) {
  return (
    <section className="rounded-xl border border-slate-800 bg-slate-900/70 p-5 shadow-lg shadow-black/20">
      {(title || extra) && (
        <header className="mb-4 flex items-center justify-between">
          <h2 className="text-sm font-semibold text-slate-300">{title}</h2>
          {extra}
        </header>
      )}
      {children}
    </section>
  )
}
