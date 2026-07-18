import { useEffect, useRef, useState } from 'react'
import { ApiError } from '../api'

/**
 * agent(交易代理) 启停按钮：启动单击即生效；停止需两段确认（3 秒内第二次点击才执行）。
 * 失败时在按钮下方展示原因。
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

  const base = 'rounded-lg px-4 py-2 text-sm font-medium transition-colors disabled:opacity-50'
  const color = running
    ? armed
      ? 'bg-amber-500 hover:bg-amber-400 text-slate-950'
      : 'bg-slate-700 hover:bg-slate-600 text-slate-100'
    : 'bg-emerald-600 hover:bg-emerald-500 text-white'
  const text = pending
    ? '执行中…'
    : running
      ? armed
        ? '再次点击确认停止'
        : '停止 agent(交易代理)'
      : '启动 agent(交易代理)'

  return (
    <div>
      <button
        type="button"
        className={`${base} ${color}`}
        disabled={pending || disabled}
        onClick={handleClick}
      >
        {text}
      </button>
      {message && <p className="mt-2 text-xs text-rose-400">{message}</p>}
    </div>
  )
}
