/**
 * 交易对白名单编辑：chips 展示 + 添加/删除，保存整体 PUT。
 * 使用紫色 chips + 虚线添加按钮。
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
            className="flex items-center gap-1 rounded-lg border border-violet-400/30 bg-violet-400/10 px-2.5 py-1 font-mono text-xs text-violet-200"
          >
            {c}
            <button
              type="button"
              aria-label={`移除 ${c}`}
              onClick={() => setContracts(contracts.filter((x) => x !== c))}
              className="ml-1 text-violet-300/50 transition hover:text-rose-400"
            >
              ×
            </button>
          </span>
        ))}
        {contracts.length === 0 && <span className="text-xs text-zinc-500">白名单为空</span>}
      </div>

      <div className="mt-3 flex items-center gap-2">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && add()}
          placeholder="输入合约名，如 SOL_USDT"
          aria-label="新合约名"
          className="w-44 rounded-lg border border-white/10 bg-zinc-900 px-3 py-1.5 font-mono text-xs text-zinc-100 focus:border-violet-400/60 focus:outline-none"
        />
        <button
          type="button"
          onClick={add}
          className="rounded-lg border border-dashed border-white/15 px-3 py-1.5 text-xs text-zinc-500 transition hover:border-violet-400/40 hover:text-violet-300"
        >
          + 添加合约
        </button>
        <button
          type="button"
          disabled={pending}
          onClick={handleSave}
          className="rounded-lg border border-violet-400/50 bg-violet-400/10 px-3 py-1.5 text-xs text-violet-300 transition hover:bg-violet-400/20 disabled:opacity-40"
        >
          {pending ? '保存中…' : '保存白名单'}
        </button>
      </div>
      {hint && <p className="mt-2 text-xs text-rose-400">{hint}</p>}
      <p className="mt-2 text-[10px] text-zinc-600">
        watchlist：风控硬校验，非白名单合约一律拒绝开仓。settle={initial.settle}
      </p>
    </div>
  )
}
