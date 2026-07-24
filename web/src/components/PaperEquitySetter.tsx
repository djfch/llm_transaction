import { useEffect, useRef, useState } from 'react'
import { api, ApiError } from '../api'

/**
 * 设置金额（仅 paper 模式展示）：数字输入 + 两段确认按钮，
 * 确认后调用 resetPaperEquity 并触发 onReset 刷新账户/权益。
 */
export default function PaperEquitySetter({ onReset }: { onReset: () => void }) {
  const [value, setValue] = useState('10000')
  const [armed, setArmed] = useState(false)
  const [pending, setPending] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)

  const amount = Number(value)
  const valid = Number.isFinite(amount) && amount > 0

  // 卸载时清理待确认计时器
  useEffect(
    () => () => {
      if (timer.current) clearTimeout(timer.current)
    },
    [],
  )

  const handleClick = async () => {
    if (pending || !valid) return
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
      const res = await api.resetPaperEquity(amount)
      setMessage(`已设置账户权益 = ${res.equity}`)
      onReset()
    } catch (e) {
      // 409 为非 paper 模式拒绝，展示后端给出的原因
      setMessage(e instanceof ApiError ? e.detail : String(e))
    } finally {
      setPending(false)
    }
  }

  return (
    <div className="flex flex-wrap items-center gap-2 text-xs text-slate-400">
      <label className="flex items-center gap-2">
        设置权益金额 USDT
        <input
          type="number"
          min="0"
          step="any"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          className="w-32 rounded-lg border border-slate-700 bg-slate-800 px-2 py-1.5 text-xs tabular-nums text-slate-200 focus:border-sky-500 focus:outline-none"
        />
      </label>
      <button
        type="button"
        disabled={pending || !valid}
        onClick={handleClick}
        className={`rounded-lg px-3 py-1.5 text-xs font-medium transition-colors disabled:opacity-50 ${
          armed
            ? 'bg-amber-500 text-slate-950 hover:bg-amber-400'
            : 'bg-slate-700 text-slate-100 hover:bg-slate-600'
        }`}
      >
        {pending ? '设置中…' : armed ? '再次点击确认设置' : '设置金额'}
      </button>
      {message && <span className="text-slate-400">{message}</span>}
    </div>
  )
}
