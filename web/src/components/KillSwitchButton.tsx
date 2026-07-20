import { useEffect, useRef, useState } from 'react'
import { ApiError } from '../api'

/**
 * kill_switch(紧急停止) 熔断按钮（方案 C 顶栏样式）：紧凑的 ⏻ 熔断 KILL。
 * 两段确认防误触：第一次点击进入"待确认"状态（3 秒内有效），第二次点击才真正切换。
 * 熔断已触发时以实心红色呈现，点击可复位（同样两段确认）。失败时展示原因。
 */
export default function KillSwitchButton({
  enabled,
  disabled = false,
  onToggle,
}: {
  enabled: boolean
  /** status 未加载完成时禁用，避免按错误的初始文案操作 */
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

  const handleClick = async () => {
    if (pending) return
    if (!armed) {
      setArmed(true)
      timer.current = setTimeout(() => setArmed(false), 3000)
      return
    }
    if (timer.current) clearTimeout(timer.current)
    setArmed(false)
    setPending(true)
    setMessage(null)
    try {
      await onToggle(!enabled)
    } catch (e) {
      setMessage(e instanceof ApiError ? e.detail : String(e))
    } finally {
      setPending(false)
    }
  }

  const base =
    'rounded-md border px-2.5 py-1 text-xs font-semibold transition disabled:opacity-50 disabled:cursor-not-allowed'
  // 常态玫瑰描边；已触发实心玫瑰；待确认琥珀警示
  const color = armed
    ? 'border-amber-400/50 bg-amber-400/10 text-amber-300'
    : enabled
      ? 'border-rose-500 bg-rose-600/80 text-white hover:bg-rose-500/80'
      : 'border-rose-500/50 text-rose-400 hover:bg-rose-500/15'
  const text = pending
    ? '执行中…'
    : armed
      ? enabled
        ? '确认复位？'
        : '确认触发熔断？'
      : enabled
        ? '⏻ KILL 已触发'
        : '⏻ 熔断 KILL'

  return (
    <div className="flex items-center gap-2">
      <button
        type="button"
        className={`${base} ${color}`}
        disabled={pending || disabled}
        onClick={handleClick}
      >
        {text}
      </button>
      {message && <p className="text-xs text-rose-400">{message}</p>}
    </div>
  )
}
