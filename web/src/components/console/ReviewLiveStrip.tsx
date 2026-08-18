/**
 * 复盘进行中进度条：复盘报告面板顶部的实时状态条，按轮次 ID 认轮（不比较任何时间）。
 * 进入进行中态三条通路：
 * ① pinned（可信绑定）：WS review_round_start，或面板点火事件 review-round-ignite
 *   （detail.roundId 为 POST /api/review/run 预分配的审计轮 ID，与 WS 轮始事件同一标识）——
 *   直接绑定该 round_id，只认这一轮：/live 按绑定 ID 直查（?round_id=），不受其他轮占位影响；
 *   查无此轮（后台尚未 begin_round 或轮不存在）走 90 秒兜底；绑定轮 ended_at 非空即退出
 *   （本轮快速结束也立即识别，不比 started_at）；绑定轮超 30 分钟未闭合（进程重启残留的
 *   永不闭合轮）认定僵尸死亡退出。
 * ② discovery（轮询发现）：仅面板 409 catchup 事件（review-round-catchup，已激活时不降级重建）——
 *   任务已预留但审计轮可能还没开，激活后靠每 3 秒轮询发现：/live 返回的非僵尸进行中轮即当前真相，
 *   见到更新轮次就换绑（修僵尸误绑卡死的关键）；绑定轮 ended_at 非空即退出；绑定轮绑定后变僵尸
 *   （进程被强杀、永不闭合）即认定死亡退出。
 * ③ 挂载 / WS 重连补漏（一次性探测）：查到进行中且非僵尸轮（started_at 距今 ≤30 分钟）才以
 *   discovery 模式激活并直接绑定该轮；查不到不激活（页面打开时不凭空亮条）。
 * 兜底：进入进行中态约 90 秒后绑定轮从未在 /live 出现过（seen=false），视为后台点火失败，
 * 退出并回调 onFinished 让列表刷出失败报告；绑定轮见过一次后跑多久都不再兜底。
 * 退出幂等：清空绑定/可信标记/seen/计时，停轮询并回调 onFinished（父组件据此刷新报告列表）。
 * 每次进入进行中态代际（generation）+1，轮询响应按发送时的代际校验，上一激活周期的迟到响应
 * 直接丢弃；轮询失败静默保留进度条。WS review_round 事件仅 round_id 与绑定相符（或未绑定）
 * 时才退出，不符忽略（防御其他轮的迟到事件误杀本周期）。
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../../api'
import type { ReviewLive, ReviewLiveRound, ToolCall } from '../../api/types'
import { useWs } from '../../hooks/useWs'

/** 轮询间隔（毫秒）：进行中每 3 秒拉一次实时复盘状态 */
const POLL_MS = 3000
/** 僵尸轮阈值（毫秒）：started_at 超过 30 分钟的「进行中」轮视为上次强杀残留的脏数据，discovery/补漏不认 */
const ZOMBIE_MS = 30 * 60 * 1000
/** 兜底期限（毫秒）：进入进行中态约 90 秒后绑定轮从未在 /live 出现过，视为后台在 begin_round 前失败 */
const FALLBACK_MS = 90 * 1000

/** 僵尸轮判定：进行中的轮 started_at 距今超过阈值（上次强杀残留的未闭合旧轮） */
function isZombieRound(round: ReviewLiveRound, nowMs: number): boolean {
  return nowMs - round.started_at * 1000 > ZOMBIE_MS
}

/** 兜底判定：绑定轮从未在 /live 出现过且已超兜底期限（后台在 begin_round 前失败） */
function fallbackExpired(seen: boolean, activatedAtMs: number, nowMs: number): boolean {
  return !seen && nowMs - activatedAtMs >= FALLBACK_MS
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
  // 绑定轮次 ID：pinned 通路进入即确定；discovery 通路首次见到非僵尸进行中轮时绑定、可见更新轮时换绑；null = 未绑定
  const boundIdRef = useRef<string | null>(null)
  // 可信绑定标记：true = pinned（WS 轮始/点火 POST roundId，只认该轮）；false = discovery（/live 发现的更新轮即真相）
  const pinnedRef = useRef(false)
  // 绑定轮是否已在 /live 出现过至少一次：见过之后任务跑多久都不触发兜底
  const seenRef = useRef(false)
  // 进入进行中态的时刻（毫秒）：仅用于 90 秒兜底判定
  const activatedAtRef = useRef(0)
  // 激活代际：每次进入进行中态 +1，轮询响应按发送时的代际校验，丢弃上一周期的迟到响应
  const generationRef = useRef(0)
  // 最新 onFinished 引用：定时器回调不随父组件重渲染捕获旧闭包
  const onFinishedRef = useRef(onFinished)
  useEffect(() => {
    onFinishedRef.current = onFinished
  }, [onFinished])

  /** 退出进行中态（幂等）：清空轮次绑定/可信标记/seen/计时；停止轮询由 active effect 清理联动；notify 时通知父组件复盘已结束 */
  const exitActive = useCallback((notify: boolean) => {
    if (!activeRef.current) return
    activeRef.current = false
    boundIdRef.current = null
    pinnedRef.current = false
    seenRef.current = false
    activatedAtRef.current = 0
    setActive(false)
    if (notify) onFinishedRef.current()
  }, [])

  /** 兜底检查：绑定轮从未在 /live 出现且已超兜底期限 → 视为点火失败，退出并通知 */
  const checkFallback = useCallback(() => {
    if (fallbackExpired(seenRef.current, activatedAtRef.current, Date.now())) exitActive(true)
  }, [exitActive])

  /** 拉一次实时复盘状态：pinned 按绑定 ID 直查（其他轮占位不影响；查无此轮走兜底；绑定轮变僵尸认定死亡退出），
   *  discovery 取最新一轮、见更新的非僵尸轮即换绑；确认绑定轮结束才退出；迟到响应按代际丢弃；失败静默 */
  const pollOnce = useCallback(async () => {
    const generation = generationRef.current
    try {
      let live: ReviewLive
      if (pinnedRef.current) {
        // pinned∧未绑定当前不可达：防御未来改动引入该窗口——本轮跳过等绑定，不退化为
        // 无参查询（无参只返回最新一轮，即「他轮占位卡死」的旧 bug 形态）
        const boundId = boundIdRef.current
        if (!boundId) return
        live = await api.getReviewLive(boundId)
      } else {
        live = await api.getReviewLive()
      }
      // 已退出（WS 结束先到）或代际已换（新周期已激活）：迟到响应直接丢弃
      if (generation !== generationRef.current || !activeRef.current) return
      const round = live.round
      if (round === null) {
        // 查无此轮/还没有任何轮：保持等待，超兜底期限视为点火失败退出
        checkFallback()
        return
      }
      if (pinnedRef.current) {
        // pinned 直查绑定 ID：round 非 null 即绑定轮本身（其他轮/僵尸轮已被按 ID 查询过滤）
        if (round.ended_at !== null) {
          // 绑定轮已结束：本轮快速结束也立即识别（按 ID 直查，不比较时间）
          exitActive(true)
          return
        }
        if (isZombieRound(round, Date.now())) {
          // 绑定轮绑定后变僵尸（进程重启残留、永不闭合）认定死亡退出——否则 seen=true 后
          // 兜底永不触发，状态条永久卡死在「进行中」
          exitActive(true)
          return
        }
        seenRef.current = true
        setToolCalls(live.tool_calls)
        return
      }
      // discovery：/live 返回的更新轮次总是当前真相
      if (round.ended_at === null) {
        if (isZombieRound(round, Date.now())) {
          // 僵尸轮：外来僵尸忽略不绑定；绑定轮绑定后变僵尸（进程被强杀、永不闭合、也不会有新轮）
          // 认定死亡退出——否则 seen=true 后兜底永不触发，状态条永久卡死在「生成中」
          if (round.round_id === boundIdRef.current) exitActive(true)
          else checkFallback()
          return
        }
        if (round.round_id !== boundIdRef.current) boundIdRef.current = round.round_id // 首次绑定或换绑更新轮
        seenRef.current = true
        setToolCalls(live.tool_calls)
        return
      }
      // 已结束轮：仅绑定轮收尾才退出；其他轮的历史记录忽略 + 兜底检查
      if (boundIdRef.current !== null && round.round_id === boundIdRef.current) exitActive(true)
      else checkFallback()
    } catch {
      // 轮询失败静默：进度条不闪错误，下次轮询再试
    }
  }, [checkFallback, exitActive])

  // 进行中：立即拉一次后每 3 秒轮询；退出/卸载时清理定时器
  useEffect(() => {
    if (!active) return
    void pollOnce()
    const timer = setInterval(() => void pollOnce(), POLL_MS)
    return () => clearInterval(timer)
  }, [active, pollOnce])

  /** 进入进行中态（pinned/discovery 共用）：代际 +1、按通路设定绑定与可信标记、重置 seen 与计时、清空旧工具链并激活轮询 */
  const enterActive = useCallback((roundId: string | null, pinned: boolean) => {
    generationRef.current += 1
    boundIdRef.current = roundId
    pinnedRef.current = pinned
    seenRef.current = false
    activatedAtRef.current = Date.now()
    setToolCalls([])
    activeRef.current = true
    setActive(true)
  }, [])

  // WS：复盘轮开始 → pinned 进入（绑定其 round_id）；轮结束 → 仅 round_id 与绑定相符（或未绑定）才退出，不符忽略
  useEffect(() => {
    if (!lastMessage) return
    if (lastMessage.type === 'review_round_start') enterActive(lastMessage.data.round_id, true)
    else if (lastMessage.type === 'review_round') {
      if (boundIdRef.current === null || lastMessage.data.round_id === boundIdRef.current) exitActive(true)
    }
  }, [lastMessage, enterActive, exitActive])

  /** 面板点火事件回调：detail.roundId 为 POST 预分配的审计轮 ID → pinned 进入；detail 缺失（防御性，理论上不发生）退化为 discovery */
  const onIgnite = useCallback(
    (event: Event) => {
      const roundId = (event as CustomEvent<{ roundId?: string }>).detail?.roundId
      if (roundId) enterActive(roundId, true)
      else enterActive(null, false)
    },
    [enterActive],
  )
  useEffect(() => {
    window.addEventListener('review-round-ignite', onIgnite)
    return () => window.removeEventListener('review-round-ignite', onIgnite)
  }, [onIgnite])

  /** 面板 409 catchup 事件回调：未激活时 → discovery 进入，靠轮询发现 + 90 秒兜底；
   *  已激活时不重建周期——pinned 周期内复点按钮收到 409 若降级为 discovery，
   *  轮龄 >30 分钟时本轮会被当僵尸处理，seen 重置后兜底误退、列表缺完成刷新 */
  const onCatchup = useCallback(() => {
    if (activeRef.current) return
    enterActive(null, false)
  }, [enterActive])
  useEffect(() => {
    window.addEventListener('review-round-catchup', onCatchup)
    return () => window.removeEventListener('review-round-catchup', onCatchup)
  }, [onCatchup])

  // mountedRef：补漏查询异步回填前确认组件仍挂载（挂载/重连两条触发路径共用）
  const mountedRef = useRef(true)
  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
    }
  }, [])

  /** 补漏查询（一次性探测）：已激活时不插手（轮询自会换绑）；查到进行中且非僵尸轮则以 discovery 模式激活并直接绑定该轮（seen=true，保留轮到的工具链）；查不到不激活；失败静默 */
  const catchUp = useCallback(() => {
    api
      .getReviewLive()
      .then((live) => {
        const round = live.round
        if (!mountedRef.current || activeRef.current || !round || round.ended_at !== null) return
        if (isZombieRound(round, Date.now())) return
        generationRef.current += 1
        boundIdRef.current = round.round_id
        pinnedRef.current = false
        seenRef.current = true // 绑定轮已在 /live 出现过：不触发兜底
        activatedAtRef.current = Date.now()
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
