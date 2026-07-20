/**
 * 决策定位（RoundFocus）：K 线买卖点 / 成交记录点击后，决策时间线据此展开并高亮对应轮卡片。
 * Provider 持有定位目标；focus(roundId) 记录目标与时间戳（同轮重复点击也靠 ts 触发监听方 effect）。
 * 时间线组件 useEffect 监听 target 变化，自行完成查找/展开/滚动/高亮。
 */
import { createContext, useCallback, useContext, useMemo, useState } from 'react'
import type { ReactElement, ReactNode } from 'react'

/** 定位目标：roundId + 触发时刻（ts 用于区分同一轮的连续点击） */
export interface RoundFocusTarget {
  roundId: string
  ts: number
}

interface RoundFocusValue {
  target: RoundFocusTarget | null
  focus: (roundId: string) => void
}

const RoundFocusContext = createContext<RoundFocusValue | null>(null)

export function RoundFocusProvider({ children }: { children: ReactNode }): ReactElement {
  const [target, setTarget] = useState<RoundFocusTarget | null>(null)
  // 空 roundId（历史/未知来源成交）直接忽略，不触发定位
  const focus = useCallback((roundId: string) => {
    if (!roundId) return
    setTarget({ roundId, ts: Date.now() })
  }, [])
  const value = useMemo(() => ({ target, focus }), [target, focus])
  return <RoundFocusContext.Provider value={value}>{children}</RoundFocusContext.Provider>
}

// 上下文模块同时导出 Provider 与 Hook 是有意为之，fast-refresh 限制在此不适用
// eslint-disable-next-line react-refresh/only-export-components
export function useRoundFocus(): RoundFocusValue {
  const ctx = useContext(RoundFocusContext)
  if (!ctx) throw new Error('useRoundFocus 必须在 <RoundFocusProvider> 内使用')
  return ctx
}
