import type { Position } from '../api/types'
import { fmtNum, fmtPrice, fmtSigned, pnlClass } from '../utils/format'
import Card from './Card'

/** 持仓卡片：单合约全字段展示（标签遵循 `变量名(含义)` 格式） */
export default function PositionCard({ position }: { position: Position }) {
  const { contract, size } = position
  const direction = size > 0 ? '多' : size < 0 ? '空' : '平'
  const dirClass =
    size > 0
      ? 'bg-emerald-500/15 text-emerald-400'
      : size < 0
        ? 'bg-rose-500/15 text-rose-400'
        : 'bg-slate-700/40 text-slate-400'

  const fields: Array<[string, string]> = [
    ['size(持仓张数)', String(size)],
    ['entry_price(开仓均价)', fmtPrice(position.entry_price)],
    ['mark_price(标记价格)', fmtPrice(position.mark_price)],
    ['leverage(杠杆倍数)', `${position.leverage}x`],
    ['liq_price(强平价格)', fmtPrice(position.liq_price)],
  ]

  return (
    <Card>
      <div className="mb-3 flex items-center justify-between">
        <span className="font-semibold text-slate-100">{contract}</span>
        <span className={`rounded px-2 py-0.5 text-xs font-medium ${dirClass}`}>{direction}</span>
      </div>
      <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
        {fields.map(([label, value]) => (
          <div key={label}>
            <dt className="text-xs text-slate-500">{label}</dt>
            <dd className="mt-0.5 tabular-nums text-slate-200">{value}</dd>
          </div>
        ))}
        <div>
          <dt className="text-xs text-slate-500">unrealised_pnl(未实现盈亏)</dt>
          <dd className={`mt-0.5 tabular-nums font-medium ${pnlClass(position.unrealised_pnl)}`}>
            {fmtSigned(position.unrealised_pnl)}
          </dd>
        </div>
        <div>
          <dt className="text-xs text-slate-500">notional(名义价值)</dt>
          <dd className="mt-0.5 tabular-nums text-slate-200">
            {fmtNum(Math.abs(size) * position.mark_price, 0)}
          </dd>
        </div>
      </dl>
    </Card>
  )
}
