/**
 * 决策时间线：轮次列表（分页 offset/limit），点击进入审计详情。
 */
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api'
import { useApiData } from '../hooks/useApiData'
import Badge from '../components/Badge'
import Card from '../components/Card'
import StateHint from '../components/StateHint'
import { fmtSigned, fmtTime, pnlClass } from '../utils/format'

const PAGE_SIZE = 10

export default function RoundsPage() {
  const [offset, setOffset] = useState(0)
  const query = useApiData(() => api.getRounds(offset, PAGE_SIZE), [offset])
  const rounds = query.data ?? []
  // 返回不足一页说明没有下一页
  const hasNext = rounds.length === PAGE_SIZE
  const page = Math.floor(offset / PAGE_SIZE) + 1

  return (
    <Card
      title="决策时间线 rounds"
      extra={<span className="text-xs text-slate-500">每轮决策可下钻完整审计链</span>}
    >
      <StateHint loading={query.loading} error={query.error} empty={rounds.length === 0}>
        <ul className="divide-y divide-slate-800">
          {rounds.map((r) => (
            <li key={r.round_id}>
              <Link
                to={`/rounds/${r.round_id}`}
                className="flex items-center gap-4 px-2 py-3 transition-colors hover:bg-slate-800/50"
              >
                <span className="w-28 shrink-0 font-mono text-xs text-sky-400">{r.round_id}</span>
                <span className="w-40 shrink-0 text-xs tabular-nums text-slate-500">
                  {fmtTime(r.started_at)}
                </span>
                <span className="w-20 shrink-0">
                  <Badge text={r.wake_source} tone={r.wake_source === '价格触发' ? 'warn' : 'neutral'} />
                </span>
                <span className="min-w-0 flex-1 truncate text-sm text-slate-300">{r.summary}</span>
                <span
                  className={`w-24 shrink-0 text-right text-sm tabular-nums ${
                    r.pnl_after == null ? 'text-slate-500' : pnlClass(r.pnl_after)
                  }`}
                >
                  {r.pnl_after == null ? '-' : fmtSigned(r.pnl_after)}
                </span>
              </Link>
            </li>
          ))}
        </ul>
      </StateHint>

      {/* 分页 */}
      <div className="mt-4 flex items-center justify-between border-t border-slate-800 pt-4 text-sm">
        <span className="text-xs text-slate-500">
          offset(偏移)={offset} · 第 {page} 页
        </span>
        <div className="space-x-2">
          <button
            type="button"
            className="rounded-lg bg-slate-700 px-3 py-1.5 text-xs text-slate-200 hover:bg-slate-600 disabled:opacity-40"
            disabled={offset === 0 || query.loading}
            onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
          >
            上一页
          </button>
          <button
            type="button"
            className="rounded-lg bg-slate-700 px-3 py-1.5 text-xs text-slate-200 hover:bg-slate-600 disabled:opacity-40"
            disabled={!hasNext || query.loading}
            onClick={() => setOffset(offset + PAGE_SIZE)}
          >
            下一页
          </button>
        </div>
      </div>
    </Card>
  )
}
