import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api'
import type { PortfolioSnapshot, WsMessage } from '../api/types'
import type { ApiQuery } from './useApiData'
import { useWs } from './useWs'

const REFRESH_INTERVAL_MS = 1000

export interface LivePortfolioQuery extends ApiQuery<PortfolioSnapshot> {
  connected: boolean
  lastMessage: WsMessage | null
  reloadImmediately: () => void
}

/** ticker 仅使组合快照失效；真正的收益始终重新读取交易网关。 */
export function useLivePortfolio(): LivePortfolioQuery {
  const [data, setData] = useState<PortfolioSnapshot | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const { connected, lastMessage } = useWs()
  const mounted = useRef(false)
  const inFlight = useRef(false)
  const pending = useRef(false)
  const forceNext = useRef(false)
  const nextAllowedAt = useRef(0)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const runRef = useRef<() => void>(() => undefined)

  const schedule = useCallback((immediate = false) => {
    pending.current = true
    forceNext.current ||= immediate
    if (immediate && timer.current !== null) {
      clearTimeout(timer.current)
      timer.current = null
    }
    if (!mounted.current || inFlight.current || timer.current !== null) return
    const delay = forceNext.current ? 0 : Math.max(0, nextAllowedAt.current - Date.now())
    timer.current = setTimeout(() => {
      timer.current = null
      runRef.current()
    }, delay)
  }, [])

  runRef.current = () => {
    if (!mounted.current || inFlight.current) return
    pending.current = false
    forceNext.current = false
    inFlight.current = true
    nextAllowedAt.current = Date.now() + REFRESH_INTERVAL_MS
    setLoading(true)
    setError(null)
    api
      .getPortfolio()
      .then((snapshot) => {
        if (mounted.current) setData(snapshot)
      })
      .catch((reason: unknown) => {
        if (mounted.current) setError(reason instanceof Error ? reason.message : String(reason))
      })
      .finally(() => {
        inFlight.current = false
        if (!mounted.current) return
        setLoading(false)
        if (pending.current) schedule(forceNext.current)
      })
  }

  useEffect(() => {
    mounted.current = true
    schedule(true)
    return () => {
      mounted.current = false
      if (timer.current !== null) clearTimeout(timer.current)
    }
  }, [schedule])

  useEffect(() => {
    if (lastMessage?.type === 'ticker') schedule()
    if (lastMessage?.type === 'round_start' || lastMessage?.type === 'round') schedule(true)
  }, [lastMessage, schedule])

  const reloadImmediately = useCallback(() => schedule(true), [schedule])
  return {
    data,
    loading,
    error,
    reload: reloadImmediately,
    reloadImmediately,
    connected,
    lastMessage,
  }
}
