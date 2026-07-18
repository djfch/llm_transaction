/**
 * K 线卡片：合约下拉（来自 watchlist）+ 周期下拉，切换即重新取数渲染 CandleChart。
 */
import { useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import { useApiData } from '../hooks/useApiData'
import CandleChart from './CandleChart'
import Card from './Card'
import StateHint from './StateHint'

/** 可选 K 线周期 */
const INTERVALS = ['1m', '5m', '15m', '1h', '4h', '1d'] as const

const selectClass =
  'rounded-lg border border-slate-700 bg-slate-800 px-2 py-1.5 text-xs text-slate-200 focus:border-sky-500 focus:outline-none'

export default function CandleCard() {
  const watchlistQ = useApiData(() => api.getWatchlist(), [])
  const [contract, setContract] = useState('')
  const [interval, setInterval] = useState<string>('1h')
  // useMemo 固定引用，避免每次渲染都生成新数组触发 effect 告警
  const contracts = useMemo(() => watchlistQ.data?.contracts ?? [], [watchlistQ.data])

  // 白名单加载完成后默认选中第一个合约
  useEffect(() => {
    if (!contract && contracts.length > 0) setContract(contracts[0])
  }, [contracts, contract])

  const candlesQ = useApiData(
    () => (contract ? api.getCandles(contract, interval, 200) : Promise.resolve([])),
    [contract, interval],
  )

  // watchlist 失败要透出错误（否则 contract 永远为空、卡片永久"加载中"）
  const loading = watchlistQ.loading || (contract !== '' && candlesQ.loading)
  const error = watchlistQ.error ?? candlesQ.error
  const empty = !loading && !error && (contract === '' || (candlesQ.data ?? []).length === 0)

  return (
    <Card
      title="K线 candles"
      extra={
        <div className="flex items-center gap-3 text-xs text-slate-400">
          <label className="flex items-center gap-2">
            contract(合约)
            <select value={contract} onChange={(e) => setContract(e.target.value)} className={selectClass}>
              {contracts.map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </select>
          </label>
          <label className="flex items-center gap-2">
            interval(周期)
            <select value={interval} onChange={(e) => setInterval(e.target.value)} className={selectClass}>
              {INTERVALS.map((i) => (
                <option key={i} value={i}>
                  {i}
                </option>
              ))}
            </select>
          </label>
        </div>
      }
    >
      <StateHint loading={loading} error={error} empty={empty}>
        {candlesQ.data && <CandleChart data={candlesQ.data} />}
      </StateHint>
    </Card>
  )
}
