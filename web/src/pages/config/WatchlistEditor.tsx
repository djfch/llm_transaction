/**
 * 交易对白名单编辑：chips 展示 + 添加/删除，保存整体 PUT。
 */
import { useState } from 'react'
import type { Watchlist } from '../../api/types'

/** 合约名格式：大写字母/数字，下划线连接，如 BTC_USDT */
const CONTRACT_RE = /^[A-Z0-9]+_[A-Z0-9]+$/

export default function WatchlistEditor({
  initial,
  onSave,
}: {
  initial: Watchlist
  onSave: (list: Watchlist) => Promise<void>
}) {
  const [contracts, setContracts] = useState<string[]>(initial.contracts)
  const [input, setInput] = useState('')
  const [hint, setHint] = useState<string | null>(null)
  const [pending, setPending] = useState(false)

  const add = () => {
    const symbol = input.trim().toUpperCase()
    if (!CONTRACT_RE.test(symbol)) {
      setHint('合约格式应为 XXX_YYY（大写），如 BTC_USDT')
      return
    }
    if (contracts.includes(symbol)) {
      setHint('该合约已在白名单中')
      return
    }
    setContracts([...contracts, symbol])
    setInput('')
    setHint(null)
  }

  const handleSave = async () => {
    setPending(true)
    setHint(null)
    try {
      await onSave({ ...initial, contracts })
    } catch (e) {
      setHint(e instanceof Error ? e.message : String(e))
    } finally {
      setPending(false)
    }
  }

  return (
    <div>
      <div className="flex flex-wrap items-center gap-2">
        {contracts.map((c) => (
          <span
            key={c}
            className="flex items-center gap-1 rounded-lg border border-slate-700 bg-slate-800 px-2.5 py-1 text-sm text-slate-200"
          >
            {c}
            <button
              type="button"
              aria-label={`移除 ${c}`}
              onClick={() => setContracts(contracts.filter((x) => x !== c))}
              className="ml-1 text-slate-500 hover:text-rose-400"
            >
              ×
            </button>
          </span>
        ))}
        {contracts.length === 0 && <span className="text-sm text-slate-500">白名单为空</span>}
      </div>

      <div className="mt-3 flex items-center gap-2">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && add()}
          placeholder="输入合约名，如 SOL_USDT"
          aria-label="新合约名"
          className="w-56 rounded-lg border border-slate-700 bg-slate-800 px-3 py-2 text-sm text-slate-100 focus:border-sky-500 focus:outline-none"
        />
        <button
          type="button"
          onClick={add}
          className="rounded-lg bg-slate-700 px-3 py-2 text-sm text-slate-200 hover:bg-slate-600"
        >
          添加
        </button>
        <button
          type="button"
          disabled={pending}
          onClick={handleSave}
          className="rounded-lg bg-sky-600 px-4 py-2 text-sm font-medium text-white hover:bg-sky-500 disabled:opacity-40"
        >
          {pending ? '保存中…' : '保存白名单'}
        </button>
      </div>
      {hint && <p className="mt-2 text-xs text-rose-400">{hint}</p>}
      <p className="mt-2 text-xs text-slate-500">
        watchlist(交易对白名单)：风控硬校验，非白名单合约一律拒绝开仓。settle={initial.settle}
      </p>
    </div>
  )
}
