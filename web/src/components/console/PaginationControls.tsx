/** 通用分页控件：最多显示五个连续页码，并提供前后翻页与指定页跳转。 */
import { useEffect, useState, type FormEvent } from 'react'

const MAX_VISIBLE_PAGES = 5

/** 分页控件需要的通用输入，不依赖任一业务数据类型。 */
interface PaginationControlsProps {
  page: number
  total: number
  pageSize: number
  itemLabel: string
  loading: boolean
  onPageChange: (page: number) => void
}

/** 计算当前页周围的连续页码窗口，始终不超过 MAX_VISIBLE_PAGES。 */
function visiblePages(currentPage: number, totalPages: number): number[] {
  const maxStart = Math.max(0, totalPages - MAX_VISIBLE_PAGES)
  const centeredStart = Math.max(0, currentPage - Math.floor(MAX_VISIBLE_PAGES / 2))
  const start = Math.min(centeredStart, maxStart)
  const end = Math.min(start + MAX_VISIBLE_PAGES, totalPages)
  return Array.from({ length: end - start }, (_, index) => start + index)
}

/** 页码按钮组：当前页以 aria-current 和紫色样式标记。 */
function PageButtons({
  currentPage,
  totalPages,
  itemLabel,
  loading,
  onPageChange,
}: Pick<PaginationControlsProps, 'itemLabel' | 'loading' | 'onPageChange'> & {
  currentPage: number
  totalPages: number
}) {
  return (
    <div className="flex flex-wrap items-center justify-center gap-1" role="navigation" aria-label={`${itemLabel}分页`}>
      {visiblePages(currentPage, totalPages).map((pageNumber) => (
        <button
          key={pageNumber}
          type="button"
          aria-current={pageNumber === currentPage ? 'page' : undefined}
          aria-label={`第 ${pageNumber + 1} 页`}
          disabled={loading}
          onClick={() => onPageChange(pageNumber)}
          className={`min-w-8 rounded-md border px-2 py-1.5 font-mono transition disabled:cursor-not-allowed disabled:opacity-50 ${
            pageNumber === currentPage
              ? 'border-violet-400/70 bg-violet-400/15 text-violet-200'
              : 'border-zinc-700 bg-zinc-900 text-zinc-400 hover:border-violet-400/50 hover:text-violet-300'
          }`}
        >
          {pageNumber + 1}
        </button>
      ))}
    </div>
  )
}

/** 一基页码跳转表单：非法输入仅显示提示，不调用 onPageChange。 */
function JumpForm({
  currentPage,
  totalPages,
  itemLabel,
  loading,
  onPageChange,
}: Pick<PaginationControlsProps, 'itemLabel' | 'loading' | 'onPageChange'> & {
  currentPage: number
  totalPages: number
}) {
  const [jumpValue, setJumpValue] = useState(String(currentPage + 1))
  const [jumpError, setJumpError] = useState('')

  useEffect(() => {
    setJumpValue(String(currentPage + 1))
    setJumpError('')
  }, [currentPage])

  /** 提交一基页码；越界或非整数时仅显示提示，绝不触发请求。 */
  const submitJump = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const requested = Number(jumpValue)
    if (!Number.isInteger(requested) || requested < 1 || requested > totalPages) {
      setJumpError(`请输入 1 至 ${totalPages} 的整数页码`)
      return
    }
    setJumpError('')
    onPageChange(requested - 1)
  }

  return (
    <div className="flex flex-wrap items-center justify-end gap-1">
      <form noValidate className="flex items-center gap-1" onSubmit={submitJump}>
        <label className="whitespace-nowrap" htmlFor={`${itemLabel}-page-jump`}>
          跳至第
        </label>
        <input
          id={`${itemLabel}-page-jump`}
          aria-label={`跳转到第几页${itemLabel}`}
          aria-invalid={jumpError ? 'true' : undefined}
          className="w-14 rounded-md border border-zinc-700 bg-zinc-900 px-2 py-1 text-center font-mono text-zinc-200 outline-none transition focus:border-violet-400"
          min="1"
          max={totalPages}
          step="1"
          type="number"
          value={jumpValue}
          disabled={loading}
          onChange={(event) => setJumpValue(event.target.value)}
        />
        <span>页</span>
        <button
          type="submit"
          disabled={loading}
          className="rounded-md border border-zinc-700 bg-zinc-900 px-2 py-1 text-zinc-200 transition hover:border-violet-400/50 hover:text-violet-300 disabled:cursor-not-allowed disabled:opacity-50"
        >
          跳转
        </button>
      </form>
      {jumpError && <p className="basis-full text-right text-rose-400" role="alert">{jumpError}</p>}
    </div>
  )
}

/** 两个监控面板共用的页码导航、页数摘要与跳转输入。 */
export default function PaginationControls({
  page,
  total,
  pageSize,
  itemLabel,
  loading,
  onPageChange,
}: PaginationControlsProps) {
  const totalPages = total > 0 ? Math.ceil(total / pageSize) : 0
  const currentPage = totalPages > 0 ? Math.min(page, totalPages - 1) : 0
  if (totalPages === 0) return null

  return (
    <footer className="mt-4 flex flex-wrap items-center justify-between gap-3 border-t border-zinc-800/80 pt-3 text-xs text-zinc-500">
      <button
        type="button"
        aria-label="上一页"
        disabled={loading || currentPage === 0}
        onClick={() => onPageChange(currentPage - 1)}
        className="rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-1.5 font-medium text-zinc-200 transition hover:border-violet-400/50 hover:text-violet-300 disabled:cursor-not-allowed disabled:opacity-50"
      >
        上一页
      </button>
      <PageButtons
        currentPage={currentPage}
        totalPages={totalPages}
        itemLabel={itemLabel}
        loading={loading}
        onPageChange={onPageChange}
      />
      <button
        type="button"
        aria-label="下一页"
        disabled={loading || currentPage >= totalPages - 1}
        onClick={() => onPageChange(currentPage + 1)}
        className="rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-1.5 font-medium text-zinc-200 transition hover:border-violet-400/50 hover:text-violet-300 disabled:cursor-not-allowed disabled:opacity-50"
      >
        下一页
      </button>
      <span className="whitespace-nowrap tabular-nums">
        第 {currentPage + 1}/{totalPages} 页 · 共 {total} 条{itemLabel}
      </span>
      <JumpForm
        currentPage={currentPage}
        totalPages={totalPages}
        itemLabel={itemLabel}
        loading={loading}
        onPageChange={onPageChange}
      />
    </footer>
  )
}
