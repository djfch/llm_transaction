/**
 * 实时决策轮当前展示 agent 的选择状态机：一次只显示一个 agent 的实时轮。
 * 规则：多个同时运行显示「最后开始」的；当前显示的结束后仍有在跑的回切到仍在跑的最后者；
 * 全部结束停留在最后结束那轮（不回切 trader）；页面打开无进行中时默认 trader（显示其上轮）。
 * 补漏为合并式（connected 跳变为 true 时触发，含首次连接与断线重连）：与 WS 先行入栈项
 * 去重合并、按 started_at 升序重排；ended_at===null 但 started_at 超 30 分钟的轮视为
 * 僵尸轮（进程崩溃残留的脏数据），不算进行中。
 */
import { useEffect, useRef, useState } from 'react'
import { api } from '../api'
import type { LiveAgentKind, WsMessage } from '../api/types'

/** 三 agent 清单：补漏的查询顺序，与 Promise.all 结果索引一一对应 */
const AGENTS: LiveAgentKind[] = ['trader', 'review', 'research']

/** 僵尸轮阈值（毫秒）：ended_at===null 但 started_at 超过 30 分钟的轮是进程崩溃残留的脏数据，不算进行中 */
export const ZOMBIE_MS = 30 * 60 * 1000

/** WS 事件 → {agent, start} 映射：三 agent 各一对开始/结束事件，共六事件 */
const WS_EVENT_AGENT: Partial<Record<WsMessage['type'], { agent: LiveAgentKind; start: boolean }>> = {
  round_start: { agent: 'trader', start: true },
  round: { agent: 'trader', start: false },
  review_round_start: { agent: 'review', start: true },
  review_round: { agent: 'review', start: false },
  research_round_start: { agent: 'research', start: true },
  research_round: { agent: 'research', start: false },
}

/** 是否实时轮相关 WS 事件（六事件之一）：消费方（hero）据此触发即时刷新 */
export function isLiveRoundEvent(msg: WsMessage): boolean {
  return WS_EVENT_AGENT[msg.type] !== undefined
}

export function useLiveAgent(lastMessage: WsMessage | null, connected: boolean): LiveAgentKind {
  // 有序活跃栈（栈尾 = 最新开始的 agent，即 current 的数据源）；
  // 用 ref 持有：补漏的异步回调才能读到 WS 先行写入的最新值
  const activeRef = useRef<LiveAgentKind[]>([])
  // 各在栈 agent 的开始时间（毫秒）：WS 入栈记事件到达时刻（近似），补漏以服务器 started_at 校准；
  // 合并重排与 WS 挪尾共用同一份口径
  const startedAtRef = useRef(new Map<LiveAgentKind, number>())
  const [current, setCurrent] = useState<LiveAgentKind>('trader')

  // WS 驱动栈操作：start 去重后挪尾（= 最新），end 移除；
  // 栈空时保持 current 不变（停留最后结束的 agent，不回切 trader）
  useEffect(() => {
    if (lastMessage === null) return
    const ev = WS_EVENT_AGENT[lastMessage.type]
    if (ev === undefined) return
    const stack = activeRef.current.filter((a) => a !== ev.agent)
    if (ev.start) {
      stack.push(ev.agent)
      startedAtRef.current.set(ev.agent, Date.now())
    } else {
      startedAtRef.current.delete(ev.agent)
    }
    activeRef.current = stack
    if (stack.length > 0) setCurrent(stack[stack.length - 1])
  }, [lastMessage])

  // 合并式补漏（connected false→true 跳变时触发，覆盖首次连接与断线重连丢 start 事件场景）：
  // 并行查三端点，进行中的轮（ended_at===null 且未超僵尸阈值）与栈内已有项（WS 先行入栈）
  // 去重合并、按 started_at 升序重排——补漏找回断线期间丢失的 start，WS 先行的信息也不丢
  useEffect(() => {
    if (!connected) return
    let alive = true
    Promise.all(AGENTS.map((a) => api.getLiveFor(a).catch(() => null))).then((snaps) => {
      if (!alive) return
      const now = Date.now()
      const stack = [...activeRef.current]
      snaps.forEach((snap, i) => {
        const round = snap?.round ?? null
        if (round === null || round.ended_at !== null) return
        if (now - round.started_at * 1000 > ZOMBIE_MS) return // 僵尸轮不入栈
        const agent = AGENTS[i]
        if (!stack.includes(agent)) stack.push(agent)
        startedAtRef.current.set(agent, round.started_at * 1000) // 服务器口径更准，校准/登记
      })
      stack.sort((a, b) => (startedAtRef.current.get(a) ?? 0) - (startedAtRef.current.get(b) ?? 0))
      activeRef.current = stack
      if (stack.length > 0) setCurrent(stack[stack.length - 1])
    })
    return () => {
      alive = false
    }
  }, [connected])

  return current
}
