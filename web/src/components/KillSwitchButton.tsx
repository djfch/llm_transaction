import { useEffect, useRef, useState } from 'react'

/**
 * kill_switch(紧急停止) 开关按钮：两段确认防误触。
 * 第一次点击进入"待确认"状态（3 秒内有效），第二次点击才真正切换。
 */
export default function KillSwitchButton({
  enabled,
  onToggle,
}: {
  enabled: boolean
  onToggle: (next: boolean) => Promise<void>
}) {
  const [armed, setArmed] = useState(false)
  const [pending, setPending] = useState(false)
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
    try {
      await onToggle(!enabled)
    } finally {
      setPending(false)
    }
  }

  const base = 'rounded-lg px-4 py-2 text-sm font-medium transition-colors disabled:opacity-50'
  const color = enabled
    ? 'bg-rose-600 hover:bg-rose-500 text-white'
    : armed
      ? 'bg-amber-500 hover:bg-amber-400 text-slate-950'
      : 'bg-slate-700 hover:bg-slate-600 text-slate-100'
  const text = pending
    ? '执行中…'
    : armed
      ? `再次点击确认${enabled ? '关闭' : '开启'}`
      : enabled
        ? '关闭 kill_switch(紧急停止)'
        : '开启 kill_switch(紧急停止)'

  return (
    <button type="button" className={`${base} ${color}`} disabled={pending} onClick={handleClick}>
      {text}
    </button>
  )
}
