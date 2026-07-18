/**
 * 交易记录：成交表格（含 source(来源) 列）+ 服务端合约筛选 + 底部分页器。
 * 切换筛选或页码都会重新请求 getTrades(offset, limit, contract)。
 */
import { useState } from 'react'
import { api } from '../api'
import { useApiData } from '../hooks/useApiData'
import Badge from '../components/Badge'
import Card from '../components/Card'
import StateHint from '../components/StateHint'
import { fmtNum, fmtPrice, fmtSigned, fmtTime, pnlClass, sourceBadge } from '../utils/format'

const ALL = '全部合约'
/** 筛选项：固定常用合约 + 全部（分页为服务端取数，无法从已加载页统计全量合约） */
const CONTRACT_OPTIONS = [ALL, 'BTC_USDT', 'ETH_USDT']
/** 每页笔数 */
const PAGE_SIZE = 20

export default function TradesPage() {
  const [contract, setContract] = useState(ALL)
  const [page, setPage] = useState(0) // 0 基页码

  const query = useApiData(
    () => api.getTrades(page * PAGE_SIZE, PAGE_SIZE, contract === ALL ? undefined : contract),
    [page, contract],
  )

  const items = query.data?.items ?? []
  const total = query.data?.total ?? 0
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE))

  // 切换筛选时回到第一页（重新请求由 deps 驱动）
  const changeContract = (next: string) => {
    setPage(0)
    setContract(next)
  }

  return (
    <Card
      title="交易记录 trades"
      extra={
        <label className="flex items-center gap-2 text-xs text-slate-400">
          contract(合约筛选)
          <select
            value={contract}
            onChange={(e) => changeContract(e.target.value)}
            className="rounded-lg border border-slate-700 bg-slate-800 px-2 py-1.5 text-xs text-slate-200 focus:border-sky-500 focus:outline-none"
          >
            {CONTRACT_OPTIONS.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </select>
        </label>
      }
    >
      <StateHint loading={query.loading} error={query.error} empty={items.length === 0}>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-slate-800 text-left text-xs text-slate-500">
                <th className="px-3 py-2 font-medium">time(时间)</th>
                <th className="px-3 py-2 font-medium">contract(合约)</th>
                <th className="px-3 py-2 text-right font-medium">size(张数)</th>
                <th className="px-3 py-2 text-right font-medium">price(成交价)</th>
                <th className="px-3 py-2 text-right font-medium">fee(手续费)</th>
                <th className="px-3 py-2 text-right font-medium">pnl(已实现盈亏)</th>
                <th className="px-3 py-2 font-medium">source(来源)</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800/60">
              {items.map((t) => {
                const badge = sourceBadge(t.source)
                return (
                  <tr key={t.id} className="hover:bg-slate-800/40">
                    <td className="px-3 py-2 text-xs tabular-nums text-slate-400">{fmtTime(t.time)}</td>
                    <td className="px-3 py-2 text-slate-200">{t.contract}</td>
                    <td className={`px-3 py-2 text-right tabular-nums ${pnlClass(t.size)}`}>
                      {t.size > 0 ? `+${t.size}` : t.size}
                    </td>
                    <td className="px-3 py-2 text-right tabular-nums text-slate-300">
                      {fmtPrice(t.price)}
                    </td>
                    <td className="px-3 py-2 text-right tabular-nums text-slate-400">{fmtNum(t.fee)}</td>
                    <td className={`px-3 py-2 text-right tabular-nums font-medium ${pnlClass(t.pnl)}`}>
                      {fmtSigned(t.pnl)}
                    </td>
                    <td className="px-3 py-2">
                      <Badge text={badge.text} tone={badge.tone} />
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
        {/* 分页器 */}
        <div className="mt-3 flex items-center justify-between border-t border-slate-800 pt-3 text-xs text-slate-500">
          <button
            type="button"
            disabled={page <= 0}
            onClick={() => setPage((p) => p - 1)}
            className="rounded-lg bg-slate-700 px-3 py-1.5 font-medium text-slate-100 transition-colors hover:bg-slate-600 disabled:opacity-50"
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
            className="rounded-lg bg-slate-700 px-3 py-1.5 font-medium text-slate-100 transition-colors hover:bg-slate-600 disabled:opacity-50"
          >
            下一页
          </button>
        </div>
      </StateHint>
    </Card>
  )
}
