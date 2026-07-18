/**
 * 交易记录：成交表格 + 按合约筛选（客户端过滤）。
 */
import { useMemo, useState } from 'react'
import { api } from '../api'
import { useApiData } from '../hooks/useApiData'
import Card from '../components/Card'
import StateHint from '../components/StateHint'
import { fmtNum, fmtPrice, fmtSigned, fmtTime, pnlClass } from '../utils/format'

const ALL = '全部合约'

export default function TradesPage() {
  const query = useApiData(() => api.getTrades(), [])
  const [contract, setContract] = useState(ALL)

  const trades = query.data
  // 从成交记录提取合约列表
  const contracts = useMemo(() => [ALL, ...new Set((trades ?? []).map((t) => t.contract))], [trades])
  const filtered = useMemo(() => {
    const list = trades ?? []
    return contract === ALL ? list : list.filter((t) => t.contract === contract)
  }, [trades, contract])
  const totalPnl = filtered.reduce((sum, t) => sum + t.pnl, 0)

  return (
    <Card
      title="交易记录 trades"
      extra={
        <label className="flex items-center gap-2 text-xs text-slate-400">
          contract(合约筛选)
          <select
            value={contract}
            onChange={(e) => setContract(e.target.value)}
            className="rounded-lg border border-slate-700 bg-slate-800 px-2 py-1.5 text-xs text-slate-200 focus:border-sky-500 focus:outline-none"
          >
            {contracts.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </select>
        </label>
      }
    >
      <StateHint loading={query.loading} error={query.error} empty={filtered.length === 0}>
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
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800/60">
              {filtered.map((t, i) => (
                <tr key={i} className="hover:bg-slate-800/40">
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
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="mt-3 border-t border-slate-800 pt-3 text-right text-xs text-slate-500">
          共 {filtered.length} 笔 · 合计 pnl(已实现盈亏){' '}
          <span className={`tabular-nums ${pnlClass(totalPnl)}`}>{fmtSigned(totalPnl)}</span>
        </div>
      </StateHint>
    </Card>
  )
}
