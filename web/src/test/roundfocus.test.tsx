/**
 * RoundFocus（决策定位）测试：focus 更新 target（roundId+ts）、同轮重复定位刷新 ts、空 roundId 忽略。
 */
import { act, renderHook } from '@testing-library/react'
import type { ReactNode } from 'react'
import { describe, expect, it } from 'vitest'
import { RoundFocusProvider, useRoundFocus } from '../hooks/useRoundFocus'

function wrapper({ children }: { children: ReactNode }) {
  return <RoundFocusProvider>{children}</RoundFocusProvider>
}

describe('useRoundFocus（决策定位）', () => {
  it('focus 后 target 更新为对应 roundId，ts 为当前时间', () => {
    const { result } = renderHook(() => useRoundFocus(), { wrapper })
    expect(result.current.target).toBeNull()
    const before = Date.now()
    act(() => result.current.focus('r1'))
    expect(result.current.target?.roundId).toBe('r1')
    expect(result.current.target?.ts).toBeGreaterThanOrEqual(before)
  })

  it('同一轮重复 focus：生成新 target 对象（保证监听方 useEffect 依赖变化）', () => {
    const { result } = renderHook(() => useRoundFocus(), { wrapper })
    act(() => result.current.focus('r1'))
    const first = result.current.target
    act(() => result.current.focus('r1'))
    expect(result.current.target?.roundId).toBe('r1')
    expect(result.current.target).not.toBe(first)
    expect(result.current.target?.ts).toBeGreaterThanOrEqual(first?.ts ?? 0)
  })

  it('空 roundId（历史/未知来源成交）直接忽略', () => {
    const { result } = renderHook(() => useRoundFocus(), { wrapper })
    act(() => result.current.focus(''))
    expect(result.current.target).toBeNull()
  })
})
