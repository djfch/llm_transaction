/**
 * 成交记录面板：服务端分页（每页 20）+ watchlist 驱动的合约筛选。
 * 七列：time/contract/size(正买负卖)/price/fee/pnl(正绿负红)/source·round 徽标；
 * source 徽标：llm_open 绿 / llm_close 蓝 / user_close 灰 / liquidation 红 / tpsl_close 紫；
 * round 徽标：round_id 非空显示 #短号(前8位)，空串灰显「-」；
 * 整行可点击（round_id 非空时 hover 高亮+指针）→ useRoundFocus().focus(round_id) 定位决策轮；
 * WS round 事件仅作失效信号 → 重拉当前页（保持 offset/筛选口径，新轮成交及时上表）。
 */
import { useEffect, useState } from 'react'
import { api } from '../../api'
import type { Trade } from '../../api/types'
import { useApiData } from '../../hooks/useApiData'
import { useRoundFocus } from '../../hooks/useRoundFocus'
import { useWs } from '../../hooks/useWs'
import StateHint from '../StateHint'
import { fmtNum, fmtPrice, fmtSigned, fmtTime, pnlClass, shortRoundId } from '../../utils/format'

const ALL = '全部合约'
/** 每页笔数（服务端分页） */
const PAGE_SIZE = 20

const selectClass =
  'rounded-lg border border-zinc-700 bg-zinc-900 px-2 py-1.5 text-xs text-zinc-200 focus:border-violet-400 focus:outline-none'

/** source(成交来源) → 徽标样式：llm_open 绿 / llm_close 蓝 / user_close 灰 / liquidation 红 / tpsl_close 紫 / 其他灰 */
function sourceBadgeClass(source: string): string {
  const base = 'inline-block rounded border px-2 py-0.5 font-mono text-[10px] font-medium'
  switch (source) {
    case 'llm_open':
      return `${base} border-emerald-400/40 bg-emerald-400/10 text-emerald-300`
    case 'llm_close':
      return `${base} border-sky-400/40 bg-sky-400/10 text-sky-300`
    case 'user_close':
      return `${base} border-zinc-600/50 bg-zinc-700/30 text-zinc-400`
    case 'liquidation':
      return `${base} border-rose-400/50 bg-rose-500/15 text-rose-300`
    case 'tpsl_close':
      return `${base} border-violet-400/40 bg-violet-400/10 text-violet-300`
    default:
      return `${base} border-zinc-700/50 bg-zinc-800/40 text-zinc-500`
  }
}

/** round 徽标：非空 → #短号（紫色，可定位）；空串 → 灰「-」 */
function RoundBadge({ roundId }: { roundId: string }) {
  if (!roundId) return <span className="ml-1.5 text-[10px] text-zinc-600">-</span>
  return (
    <span className="ml-1.5 rounded border border-violet-400/40 bg-violet-400/10 px-1.5 py-0.5 font-mono text-[10px] text-violet-300">
      #{shortRoundId(roundId)}
    </span>
  )
}

/** 单行成交：round_id 非空时整行可点（hover 高亮+指针），点击定位决策轮 */
function TradeRow({ trade: t, onFocus }: { trade: Trade; onFocus: (roundId: string) => void }) {
  const clickable = t.round_id !== ''
  return (
    <tr
      onClick={clickable ? () => onFocus(t.round_id) : undefined}
      title={clickable ? `点击定位到决策轮 ${t.round_id}` : undefined}
      className={clickable ? 'cursor-pointer transition hover:bg-violet-400/[.06]' : ''}
    >
      <td className="px-3 py-2 text-xs text-zinc-400">{fmtTime(t.time)}</td>
      <td className="px-3 py-2 text-zinc-200">{t.contract}</td>
      <td className={`px-3 py-2 text-right ${pnlClass(t.size)}`}>{t.size > 0 ? `+${t.size}` : t.size}</td>
      <td className="px-3 py-2 text-right text-zinc-200">{fmtPrice(t.price)}</td>
      <td className="px-3 py-2 text-right text-zinc-500">{fmtNum(t.fee)}</td>
      <td className={`px-3 py-2 text-right font-medium ${pnlClass(t.pnl)}`}>{fmtSigned(t.pnl)}</td>
      <td className="px-3 py-2 whitespace-nowrap">
        <span className={sourceBadgeClass(t.source)}>{t.source || '-'}</span>
        <RoundBadge roundId={t.round_id} />
      </td>
    </tr>
  )
}

export default function TradesTable() {
  const { focus } = useRoundFocus()
  const watchlistQ = useApiData(() => api.getWatchlist(), [])
  const [contract, setContract] = useState(ALL)
  const [page, setPage] = useState(0) // 0 基页码

  const query = useApiData(
    () => api.getTrades(page * PAGE_SIZE, PAGE_SIZE, contract === ALL ? undefined : contract),
    [page, contract],
  )

  // WS round 事件：仅作失效信号，重拉当前页（payload 不是成交数据，见 api/types.ts 契约）
  const { lastMessage } = useWs()
  const { reload } = query
  useEffect(() => {
    if (lastMessage?.type === 'round') reload()
  }, [lastMessage, reload])

  const items = query.data?.items ?? []
  const total = query.data?.total ?? 0
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE))
  const contracts = watchlistQ.data?.contracts ?? []

  // watchlist 失败要透出错误（否则筛选永远只有「全部合约」）
  const loading = query.loading || watchlistQ.loading
  const error = watchlistQ.error ?? query.error

  // 切换筛选时回到第一页（重新请求由 deps 驱动）
  const changeContract = (next: string) => {
    setPage(0)
    setContract(next)
  }

  return (
    <section className="rounded-xl border border-zinc-800 bg-zinc-950/80 p-5 shadow-lg shadow-black/30">
      <header className="mb-4 flex flex-wrap items-center gap-3">
        <h2 className="text-sm font-semibold text-zinc-200">成交记录</h2>
        <span className="text-xs text-zinc-500">size 正买负卖 · pnl 为已实现盈亏 · 点击行定位到对应决策轮</span>
        <label className="ml-auto flex items-center gap-2 text-xs text-zinc-500">
          合约筛选
          <select value={contract} onChange={(e) => changeContract(e.target.value)} className={selectClass}>
            {[ALL, ...contracts].map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </select>
        </label>
      </header>

      <StateHint loading={loading} error={error} empty={items.length === 0}>
        <div className="overflow-x-auto">
          <table className="w-full min-w-[860px] text-sm">
            <thead>
              <tr className="border-b border-zinc-800 text-left text-[11px] text-zinc-500">
                <th className="px-3 py-2 font-medium">时间</th>
                <th className="px-3 py-2 font-medium">合约</th>
                <th className="px-3 py-2 text-right font-medium">数量</th>
                <th className="px-3 py-2 text-right font-medium">成交价</th>
                <th className="px-3 py-2 text-right font-medium">手续费</th>
                <th className="px-3 py-2 text-right font-medium">已实现盈亏</th>
                <th className="px-3 py-2 font-medium">来源 · 决策轮</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-zinc-800/60 font-mono text-[13px] tabular-nums">
              {items.map((t) => (
                <TradeRow key={t.id} trade={t} onFocus={focus} />
              ))}
            </tbody>
          </table>
        </div>

        {/* 分页器（与现有页面同模式：上一页/下一页 + 页码统计） */}
        <div className="mt-3 flex items-center justify-between border-t border-zinc-800 pt-3 text-xs text-zinc-500">
          <button
            type="button"
            disabled={page <= 0}
            onClick={() => setPage((p) => p - 1)}
            className="rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-1.5 font-medium text-zinc-200 transition hover:border-violet-400/50 hover:text-violet-300 disabled:opacity-50"
          >
            上一页
          </button>
          <span className="tabular-nums">
            第 {page + 1}/{totalPages} 页 · 共 {total} 笔
          </span>
          <button
            type="button"
            disabled={page + 1 >= totalPages}
            onClick={() => setPage((p) => p + 1)}
            className="rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-1.5 font-medium text-zinc-200 transition hover:border-violet-400/50 hover:text-violet-300 disabled:opacity-50"
          >
            下一页
          </button>
        </div>
      </StateHint>
    </section>
  )
}
