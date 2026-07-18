import type { ReactNode } from 'react'

/** 数据加载状态提示：加载中 / 加载失败 / 空数据，正常时渲染 children */
export default function StateHint({
  loading,
  error,
  empty = false,
  children,
}: {
  loading: boolean
  error: string | null
  empty?: boolean
  children: ReactNode
}) {
  if (loading) return <p className="py-8 text-center text-sm text-slate-500">加载中…</p>
  if (error)
    return <p className="py-8 text-center text-sm text-rose-400">加载失败：{error}</p>
  if (empty) return <p className="py-8 text-center text-sm text-slate-500">暂无数据</p>
  return <>{children}</>
}
