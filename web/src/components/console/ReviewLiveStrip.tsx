/**
 * 复盘进行中进度条：复盘报告面板顶部的实时状态条。
 * 进入进行中态（三条通路）：WS 收到 review_round_start（直接绑定其 round_id）；面板点火事件
 * review-round-ignite（WS 断线窗口内手动点火的兜底，不依赖 WS；先记点火时刻，待 /live 首次返回
 * 进行中轮时绑定其 round_id）；挂载 / WS 重连 / 面板 409 catchup 事件（review-round-catchup）补漏
 * 发现进行中的复盘轮（绑定该轮 round_id；started_at 距今 ≤30 分钟，超出的视为僵尸轮不展示）。
 * 进行中每 3 秒轮询 /api/review/live 刷新工具链；每次进入进行中态代际（generation）+1，
 * 轮询响应按发送时的代际校验，上一激活周期的迟到响应直接丢弃。
 * 退出进行中态：WS 收到 review_round（事件不重放，收到的必为当期，收到即退出），或轮询确认
 * 本轮已结束——已绑定轮次时仅 round_id 相等才算本轮；点火路径未绑定时仅 started_at 不早于点火
 * 时刻才算本轮快速结束；否则视为上一轮的历史记录，保持等待不退出。点火后约 90 秒仍未在 /live
 * 见到进行中轮视为后台点火失败，退出并回调 onFinished 让列表刷出失败报告。退出时清空轮次绑定
 * 与点火时刻、停止轮询并回调 onFinished（父组件据此刷新报告列表）。轮询失败静默保留进度条。
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../../api'
import type { ReviewLiveRound, ToolCall } from '../../api/types'
import { useWs } from '../../hooks/useWs'

/** 轮询间隔（毫秒）：进行中每 3 秒拉一次实时复盘状态 */
const POLL_MS = 3000
/** 僵尸轮阈值（毫秒）：挂载补漏时 started_at 超过 30 分钟的「进行中」轮视为脏数据，不进入进行中态 */
const ZOMBIE_MS = 30 * 60 * 1000
/** 点火兜底期限（毫秒）：点火后超过约 90 秒仍未在 /live 见到进行中轮，视为后台在 begin_round 前失败 */
const IGNITE_GRACE_MS = 90 * 1000

/** 判定已结束轮是否属于当前激活周期：已绑定轮次按 round_id 相等；点火路径未绑定时按 started_at 不早于点火时刻（本轮快速结束） */
function isOwnEndedRound(round: ReviewLiveRound, boundId: string | null, ignitedAtMs: number | null): boolean {
  if (boundId !== null) return round.round_id === boundId
  return ignitedAtMs !== null && round.started_at * 1000 >= ignitedAtMs
}

/** 点火兜底判定：点火路径下尚未绑定到进行中轮且已超过兜底期限（后台在 begin_round 前失败） */
function igniteExpired(boundId: string | null, ignitedAtMs: number | null): boolean {
  return boundId === null && ignitedAtMs !== null && Date.now() - ignitedAtMs >= IGNITE_GRACE_MS
}

interface ReviewLiveStripProps {
  onFinished: () => void // 复盘结束回调（父组件用来刷新报告列表）
}

export default function ReviewLiveStrip({ onFinished }: ReviewLiveStripProps) {
  const { connected, lastMessage } = useWs()
  const [active, setActive] = useState(false)
  const [toolCalls, setToolCalls] = useState<ToolCall[]>([])
  // activeRef 与 active 同步：轮询/WS 双通道退出时经它去重，避免 onFinished 重复触发
  const activeRef = useRef(false)
  // 本轮 round_id 绑定：WS start 直接绑定；点火路径待 /live 首次返回进行中轮时绑定；null = 未绑定
  const roundIdRef = useRef<string | null>(null)
  // 点火时刻（毫秒）：仅点火路径记录，用于「本轮快速结束」判定与点火兜底期限
  const ignitedAtRef = useRef<number | null>(null)
  // 激活代际：每次进入进行中态 +1，轮询响应按发送时的代际校验，丢弃上一周期的迟到响应
  const generationRef = useRef(0)
  // 最新 onFinished 引用：定时器回调不随父组件重渲染捕获旧闭包
  const onFinishedRef = useRef(onFinished)
  useEffect(() => {
    onFinishedRef.current = onFinished
  }, [onFinished])

  /** 退出进行中态（幂等）：清空轮次绑定与点火时刻；停止轮询由 active effect 清理联动；notify 时通知父组件复盘已结束 */
  const exitActive = useCallback((notify: boolean) => {
    if (!activeRef.current) return
    activeRef.current = false
    roundIdRef.current = null
    ignitedAtRef.current = null
    setActive(false)
    if (notify) onFinishedRef.current()
  }, [])

  /** 拉一次实时复盘状态：进行中轮刷新工具链（未绑定则绑定）；确认本轮结束才退出（旧轮历史记录保持等待，点火路径有兜底期限）；迟到响应按代际丢弃；失败静默 */
  const pollOnce = useCallback(async () => {
    const generation = generationRef.current
    try {
      const live = await api.getReviewLive()
      // 已退出（WS 结束先到）或代际已换（新周期已激活）：迟到响应直接丢弃
      if (generation !== generationRef.current || !activeRef.current) return
      const round = live.round
      if (round === null) {
        // 无复盘轮：保持等待；点火路径超兜底期限视为点火失败退出
        if (igniteExpired(roundIdRef.current, ignitedAtRef.current)) exitActive(true)
        return
      }
      if (round.ended_at === null) {
        if (roundIdRef.current === null) roundIdRef.current = round.round_id // 点火路径首次见到进行中轮：绑定轮次
        setToolCalls(live.tool_calls)
        return
      }
      if (isOwnEndedRound(round, roundIdRef.current, ignitedAtRef.current)) {
        exitActive(true)
        return
      }
      // 上一轮的历史记录：保持等待不退出；点火路径超兜底期限仍不见新轮则视为点火失败退出
      if (igniteExpired(roundIdRef.current, ignitedAtRef.current)) exitActive(true)
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

  /** 进入进行中态（WS start 与面板点火共用）：代际 +1、绑定或待绑定轮次（roundId 为 null 记点火时刻）、清空旧工具链并激活轮询 */
  const enterActive = useCallback((roundId: string | null) => {
    generationRef.current += 1
    roundIdRef.current = roundId
    ignitedAtRef.current = roundId === null ? Date.now() : null
    setToolCalls([])
    activeRef.current = true
    setActive(true)
  }, [])

  // WS：复盘轮开始 → 进入进行中态（绑定其 round_id）；轮结束 → 退出并通知（事件不重放，收到的必为当期）
  useEffect(() => {
    if (!lastMessage) return
    if (lastMessage.type === 'review_round_start') enterActive(lastMessage.data.round_id)
    else if (lastMessage.type === 'review_round') exitActive(true)
  }, [lastMessage, enterActive, exitActive])

  /** 面板点火事件回调：WS 断线窗口内手动点火时不依赖 WS 的激活通路（与 review_round_start 同语义，但轮次待 /live 绑定） */
  const onIgnite = useCallback(() => enterActive(null), [enterActive])
  useEffect(() => {
    window.addEventListener('review-round-ignite', onIgnite)
    return () => window.removeEventListener('review-round-ignite', onIgnite)
  }, [onIgnite])

  // mountedRef：补漏查询异步回填前确认组件仍挂载（挂载/重连/catchup 三条触发路径共用）
  const mountedRef = useRef(true)
  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
    }
  }, [])

  /** 补漏查询：进行中且非僵尸轮则进入进行中态并绑定其 round_id（保留轮到的工具链）；查询失败静默视为无进行中复盘 */
  const catchUp = useCallback(() => {
    api
      .getReviewLive()
      .then((live) => {
        const round = live.round
        if (!mountedRef.current || !round || round.ended_at !== null) return
        if (Date.now() - round.started_at * 1000 > ZOMBIE_MS) return
        generationRef.current += 1
        roundIdRef.current = round.round_id
        ignitedAtRef.current = null
        activeRef.current = true
        setToolCalls(live.tool_calls)
        setActive(true)
      })
      .catch(() => {})
  }, [])

  // 挂载补漏：页面打开时复盘可能已在跑（错过 review_round_start），一次性查询
  useEffect(() => {
    catchUp()
  }, [catchUp])

  // 面板 409 catchup 事件：任务被他处（别的标签页/自动调度）点火而本页 WS 断线时，经补漏找回进行中轮
  useEffect(() => {
    window.addEventListener('review-round-catchup', catchUp)
    return () => window.removeEventListener('review-round-catchup', catchUp)
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
      data-testid="review-live-strip"
      className="mb-3 flex items-center gap-2 rounded-lg border border-dashed border-violet-400/50 bg-violet-400/5 px-3 py-2 text-xs text-violet-300"
    >
      <span className="relative flex h-2 w-2">
        <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-violet-400 opacity-75" />
        <span className="relative inline-flex h-2 w-2 rounded-full bg-violet-400" />
      </span>
      <span>
        {last ? `复盘进行中 · 已调用 ${toolCalls.length} 个工具 · 最近：${last.tool}` : '复盘进行中 · 等待 LLM 发起调用…'}
      </span>
      <span className="ml-auto text-[10px] text-zinc-500">每 3 秒自动刷新</span>
    </div>
  )
}
