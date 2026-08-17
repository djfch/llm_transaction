/**
 * 研报进行中进度条：研报面板顶部的实时状态条。
 * 进入进行中态（三条通路）：WS 收到 research_round_start；面板点火事件 research-round-ignite
 * （WS 断线窗口内手动点火的兜底，不依赖 WS）；挂载或 WS 重连补漏发现进行中的研报轮
 * （started_at 距今 ≤30 分钟，超出的视为僵尸轮不展示）。进行中每 3 秒轮询 /api/research/live 刷新工具链。
 * 退出进行中态：WS 收到 research_round，或轮询发现 round.ended_at 非空；
 * 退出时停止轮询并回调 onFinished（父组件据此刷新研报列表）。轮询失败静默保留进度条。
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../../api'
import type { ToolCall } from '../../api/types'
import { useWs } from '../../hooks/useWs'

/** 轮询间隔（毫秒）：进行中每 3 秒拉一次实时研报状态 */
const POLL_MS = 3000
/** 僵尸轮阈值（毫秒）：挂载补漏时 started_at 超过 30 分钟的「进行中」轮视为脏数据，不进入进行中态 */
const ZOMBIE_MS = 30 * 60 * 1000

interface ResearchLiveStripProps {
  onFinished: () => void // 研报结束回调（父组件用来刷新研报列表）
}

export default function ResearchLiveStrip({ onFinished }: ResearchLiveStripProps) {
  const { connected, lastMessage } = useWs()
  const [active, setActive] = useState(false)
  const [toolCalls, setToolCalls] = useState<ToolCall[]>([])
  // activeRef 与 active 同步：轮询/WS 双通道退出时经它去重，避免 onFinished 重复触发
  const activeRef = useRef(false)
  // 最新 onFinished 引用：定时器回调不随父组件重渲染捕获旧闭包
  const onFinishedRef = useRef(onFinished)
  useEffect(() => {
    onFinishedRef.current = onFinished
  }, [onFinished])

  /** 退出进行中态（幂等）：停止轮询由 active effect 清理联动；notify 时通知父组件研报已结束 */
  const exitActive = useCallback((notify: boolean) => {
    if (!activeRef.current) return
    activeRef.current = false
    setActive(false)
    if (notify) onFinishedRef.current()
  }, [])

  /** 拉一次实时研报状态：进行中刷新工具链；发现已结束则退出并通知；失败静默保留进度条 */
  const pollOnce = useCallback(async () => {
    try {
      const live = await api.getResearchLive()
      if (!activeRef.current) return // WS 结束事件先到达，忽略迟到响应
      const round = live.round
      if (round && round.ended_at === null) setToolCalls(live.tool_calls)
      else if (round) exitActive(true)
      // round 为 null（无研报轮）时保持现状，等下一轮轮询或 WS 事件
    } catch {
      // 轮询失败静默：进度条不闪错误，下次轮询再试
    }
  }, [exitActive])

  // 进行中：立即拉一次后每 3 秒轮询；退出/卸载时清理定时器
  useEffect(() => {
    if (!active) return
    void pollOnce()
    const timer = setInterval(() => void pollOnce(), POLL_MS)
    return () => clearInterval(timer)
  }, [active, pollOnce])

  /** 进入进行中态（WS start 事件与面板点火事件共用）：清空旧工具链并激活轮询 */
  const enterActive = useCallback(() => {
    setToolCalls([])
    activeRef.current = true
    setActive(true)
  }, [])

  // WS：研报轮开始 → 进入进行中态；轮结束 → 退出并通知
  useEffect(() => {
    if (!lastMessage) return
    if (lastMessage.type === 'research_round_start') enterActive()
    else if (lastMessage.type === 'research_round') exitActive(true)
  }, [lastMessage, enterActive, exitActive])

  // 面板点火事件：WS 断线窗口内手动点火时不依赖 WS 的激活通路（与 research_round_start 同语义）
  useEffect(() => {
    window.addEventListener('research-round-ignite', enterActive)
    return () => window.removeEventListener('research-round-ignite', enterActive)
  }, [enterActive])

  // mountedRef：补漏查询异步回填前确认组件仍挂载（挂载/重连两条触发路径共用）
  const mountedRef = useRef(true)
  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
    }
  }, [])

  /** 补漏查询：进行中且非僵尸轮则进入进行中态（保留轮到的工具链）；查询失败静默视为无进行中研报 */
  const catchUp = useCallback(() => {
    api
      .getResearchLive()
      .then((live) => {
        const round = live.round
        if (!mountedRef.current || !round || round.ended_at !== null) return
        if (Date.now() - round.started_at * 1000 > ZOMBIE_MS) return
        activeRef.current = true
        setToolCalls(live.tool_calls)
        setActive(true)
      })
      .catch(() => {})
  }, [])

  // 挂载补漏：页面打开时研报可能已在跑（错过 research_round_start），一次性查询
  useEffect(() => {
    catchUp()
  }, [catchUp])

  // WS 重连补漏：断线期间自动调度点火的 start 事件不重放，connected 翻 true 时重查一次
  const wasConnectedRef = useRef(connected)
  useEffect(() => {
    const was = wasConnectedRef.current
    wasConnectedRef.current = connected
    if (connected && !was) catchUp()
  }, [connected, catchUp])

  if (!active) return null
  const last = toolCalls[toolCalls.length - 1]
  return (
    <div
      data-testid="research-live-strip"
      className="mb-3 flex items-center gap-2 rounded-lg border border-dashed border-violet-400/50 bg-violet-400/5 px-3 py-2 text-xs text-violet-300"
    >
      <span className="relative flex h-2 w-2">
        <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-violet-400 opacity-75" />
        <span className="relative inline-flex h-2 w-2 rounded-full bg-violet-400" />
      </span>
      <span>
        {last ? `研报生成中 · 已调用 ${toolCalls.length} 个工具 · 最近：${last.tool}` : '研报生成中 · 等待 LLM 发起调用…'}
      </span>
      <span className="ml-auto text-[10px] text-zinc-500">每 3 秒自动刷新</span>
    </div>
  )
}
