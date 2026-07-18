/**
 * 通用数据获取 hook：管理 loading / error / data 状态，reload 可手动刷新。
 * deps 变化时自动重新加载；fetcher 经 ref 保持最新，避免闭包过期。
 */
import { useCallback, useEffect, useRef, useState } from 'react'

export interface ApiQuery<T> {
  data: T | null
  loading: boolean
  error: string | null
  reload: () => void
}

export function useApiData<T>(fetcher: () => Promise<T>, deps: unknown[] = []): ApiQuery<T> {
  const [data, setData] = useState<T | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [tick, setTick] = useState(0)

  const fetcherRef = useRef(fetcher)
  fetcherRef.current = fetcher

  const reload = useCallback(() => setTick((t) => t + 1), [])

  useEffect(() => {
    let alive = true
    setLoading(true)
    setError(null)
    fetcherRef
      .current()
      .then((result) => {
        if (alive) setData(result)
      })
      .catch((e: unknown) => {
        if (alive) setError(e instanceof Error ? e.message : String(e))
      })
      .finally(() => {
        if (alive) setLoading(false)
      })
    return () => {
      alive = false
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- deps 由调用方声明，fetcher 经 ref 保持最新
  }, [...deps, tick])

  return { data, loading, error, reload }
}
