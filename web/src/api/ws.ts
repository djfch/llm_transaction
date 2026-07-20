/**
 * WebSocket 订阅：真实模式连 /ws（经 vite proxy 转发），mock 模式定时推送假消息。
 * subscribeWs 返回取消订阅函数；onStatus 回调上报连接状态（供页头徽标显示）。
 */
import { USE_MOCK } from './index'
import type { Position, WsMessage } from './types'

export type WsHandler = (msg: WsMessage) => void
export type WsStatusHandler = (connected: boolean) => void

/** mock 模式：每 3 秒推一条 ticker，每 15 秒推一条持仓变动 */
function subscribeMock(handler: WsHandler, onStatus?: WsStatusHandler): () => void {
  onStatus?.(true)
  let tick = 0
  const timer = setInterval(() => {
    tick += 1
    if (tick % 5 === 0) {
      const position: Position = {
        contract: 'BTC_USDT',
        size: 12,
        entry_price: 118_320,
        mark_price: 119_650 + tick,
        leverage: 3,
        margin: 47.86,
        unrealised_pnl: 159.6 + tick,
        liq_price: 82_400,
      }
      handler({ type: 'position', data: position })
    } else {
      handler({
        type: 'ticker',
        data: { contract: 'BTC_USDT', last: 119_650 + Math.round(Math.random() * 200 - 100) },
      })
    }
  }, 3000)
  return () => clearInterval(timer)
}

/** 真实模式：连接同源 /ws，断线指数退避重连（3s→6s→…→30s 封顶，连上后重置）。
 * 后端未启动时避免固定间隔重试把 vite proxy 错误刷满控制台。
 * 首次连接推迟一个事件循环 tick：React StrictMode 开发模式会"挂载→立刻卸载→再挂载"，
 * 立即连接会让浏览器掐断握手（控制台报 closed before established + vite 报 ECONNABORTED）；
 * 推迟后首个 tick 的清理函数会取消这次连接，第二次（真实）挂载才发起。
 */
function subscribeReal(handler: WsHandler, onStatus?: WsStatusHandler): () => void {
  let stopped = false
  let ws: WebSocket | null = null
  let retry: ReturnType<typeof setTimeout> | null = null
  let delay = 3000

  const connect = () => {
    if (stopped) return
    const scheme = location.protocol === 'https:' ? 'wss' : 'ws'
    ws = new WebSocket(`${scheme}://${location.host}/ws`)
    ws.onopen = () => {
      delay = 3000 // 连上后重置退避
      onStatus?.(true)
    }
    ws.onmessage = (ev) => {
      try {
        handler(JSON.parse(ev.data as string) as WsMessage)
      } catch {
        // 忽略无法解析的消息
      }
    }
    ws.onclose = () => {
      onStatus?.(false)
      if (!stopped) {
        retry = setTimeout(connect, delay)
        delay = Math.min(delay * 2, 30_000)
      }
    }
  }
  retry = setTimeout(connect, 0) // 见函数头注释：推迟首连，兼容 StrictMode 双挂载

  return () => {
    stopped = true
    if (retry) clearTimeout(retry)
    ws?.close()
  }
}

export function subscribeWs(handler: WsHandler, onStatus?: WsStatusHandler): () => void {
  return USE_MOCK ? subscribeMock(handler, onStatus) : subscribeReal(handler, onStatus)
}
