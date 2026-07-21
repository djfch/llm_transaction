/**
 * 分页状态 hook：统一处理零基页码、总页数及数据缩减后的页码回退。
 * 数据请求仍由调用面板负责，避免把业务接口耦合进通用状态层。
 */
import { useCallback, useEffect, useMemo, useState } from 'react'

/** 根据 total(总数) 与 pageSize(单页条数) 计算实际页数，空列表为 0 页。 */
export function pageCountOf(total: number, pageSize: number): number {
  return total > 0 ? Math.ceil(total / pageSize) : 0
}

/** 管理当前零基页码；总数减少导致越界时自动跳回最后一个有效页。 */
export function usePageState(total: number, pageSize: number) {
  const [page, setPage] = useState(0)
  const totalPages = useMemo(() => pageCountOf(total, pageSize), [total, pageSize])

  useEffect(() => {
    setPage((current) => {
      if (totalPages === 0) return 0
      return Math.min(current, totalPages - 1)
    })
  }, [totalPages])

  /** 仅接受有效的零基页码，非法值保持当前页并避免无效请求。 */
  const goToPage = useCallback(
    (nextPage: number) => {
      if (Number.isInteger(nextPage) && nextPage >= 0 && nextPage < totalPages) setPage(nextPage)
    },
    [totalPages],
  )

  return { page, totalPages, goToPage }
}
