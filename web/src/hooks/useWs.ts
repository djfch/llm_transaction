/**
 * React 版 WS 订阅 hook：返回最新消息与连接状态。
 */
import { useEffect, useState } from 'react'
import { subscribeWs } from '../api/ws'
import type { WsMessage } from '../api/types'

export interface WsState {
  connected: boolean // 是否已连接（mock 模式恒为 true）
  lastMessage: WsMessage | null // 最近一条推送
}

export function useWs(): WsState {
  const [state, setState] = useState<WsState>({ connected: false, lastMessage: null })

  useEffect(() => {
    const unsubscribe = subscribeWs(
      (msg) => setState((s) => ({ ...s, lastMessage: msg })),
      (connected) => setState((s) => ({ ...s, connected })),
    )
    return unsubscribe
  }, [])

  return state
}
