import { useEffect, useRef, useState } from 'react'
import { ApiError } from '../api'

/**
 * agent(交易代理) 启停控件：状态指示灯 + 文字 + 紧凑切换按钮。
 * 启动单击即生效；停止需两段确认（3 秒内第二次点击才执行）。
 * 失败时在控件下方展示原因。
 */
export default function AgentControl({
  running,
  disabled = false,
  onToggle,
}: {
  running: boolean
  /** status 未加载完成时禁用，避免展示与真实状态相反的文案 */
  disabled?: boolean
  onToggle: (next: boolean) => Promise<void>
}) {
  const [armed, setArmed] = useState(false)
  const [pending, setPending] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)

  // 卸载时清理待确认计时器
  useEffect(
    () => () => {
      if (timer.current) clearTimeout(timer.current)
    },
    [],
  )

  const execute = async () => {
    setPending(true)
    setMessage(null)
    try {
      await onToggle(!running)
    } catch (e) {
      setMessage(e instanceof ApiError ? e.detail : String(e))
    } finally {
      setPending(false)
    }
  }

  const handleClick = async () => {
    if (pending) return
    if (!running) return execute() // 启动：单击直接生效
    if (!armed) {
      setArmed(true)
      timer.current = setTimeout(() => setArmed(false), 3000)
      return
    }
    if (timer.current) clearTimeout(timer.current)
    setArmed(false)
    await execute()
  }

  // 切换按钮文案与配色：停止 hover 转红（危险），启动 hover 转绿，待确认琥珀警示
  const btnBase =
    'rounded-md border px-2 py-1 text-[11px] transition disabled:opacity-50 disabled:cursor-not-allowed'
  const btnColor = armed
    ? 'border-amber-400/50 bg-amber-400/10 text-amber-300'
    : running
      ? 'border-white/10 text-zinc-400 hover:border-rose-400/40 hover:text-rose-300'
      : 'border-white/10 text-zinc-400 hover:border-emerald-400/40 hover:text-emerald-300'
  const btnText = pending ? '执行中…' : armed ? '确认停止？' : running ? '停止' : '启动'

  return (
    <div className="flex items-center gap-2">
      {/* 状态指示灯：运行中绿色脉冲，停止灰色静止 */}
      <span className="relative flex h-2.5 w-2.5">
        {running && (
          <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-400 opacity-60 motion-reduce:animate-none" />
        )}
        <span
          className={`relative inline-flex h-2.5 w-2.5 rounded-full ${running ? 'bg-emerald-400' : 'bg-zinc-600'}`}
        />
      </span>
      <span className={`text-xs ${running ? 'text-emerald-300' : 'text-zinc-500'}`}>
        {running ? 'Agent 运行中' : 'Agent 已停止'}
      </span>
      <button
        type="button"
        className={`${btnBase} ${btnColor}`}
        disabled={pending || disabled}
        onClick={handleClick}
      >
        {btnText}
      </button>
      {message && <p className="text-xs text-rose-400">{message}</p>}
    </div>
  )
}
