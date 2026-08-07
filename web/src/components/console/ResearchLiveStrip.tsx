/**
 * 研报进行中进度条：研报面板顶部的实时状态条。
 * 进入进行中态：WS 收到 research_round_start，或挂载补漏发现进行中的研报轮
 * （started_at 距今 ≤30 分钟，超出的视为僵尸轮不展示）；进行中每 3 秒轮询 /api/research/live 刷新工具链。
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
  const { lastMessage } = useWs()
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

  // WS：研报轮开始 → 进入进行中态（清空旧工具链）；轮结束 → 退出并通知
  useEffect(() => {
    if (!lastMessage) return
    if (lastMessage.type === 'research_round_start') {
      setToolCalls([])
      activeRef.current = true
      setActive(true)
    } else if (lastMessage.type === 'research_round') {
      exitActive(true)
    }
  }, [lastMessage, exitActive])

  // 挂载补漏：页面打开时研报可能已在跑（错过 research_round_start）；
  // 一次性查询，进行中且非僵尸轮则直接进入进行中态；查询失败静默视为无进行中研报
  useEffect(() => {
    let alive = true
    api
      .getResearchLive()
      .then((live) => {
        const round = live.round
        if (!alive || !round || round.ended_at !== null) return
        if (Date.now() - round.started_at * 1000 > ZOMBIE_MS) return
        activeRef.current = true
        setToolCalls(live.tool_calls)
        setActive(true)
      })
      .catch(() => {})
    return () => {
      alive = false
    }
  }, [])

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
