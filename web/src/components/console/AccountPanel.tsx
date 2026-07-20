/**
 * 账户面板（方案 C 换皮）：equity(账户权益) 大数字 + 累计涨跌行 + available / unrealised_pnl 双格
 * + 底部「今日已实现 / 当日开仓单」行，等宽数字 tabular-nums，盈亏正绿负红。
 * paper 权益重置已挪入配置抽屉（ConfigDrawer），本面板只读展示。
 * 数据由父级装配层下发（哑组件）；当日统计为后端 /api/daily_stats 风控口径。
 */
import type { AccountInfo, DailyStats } from '../../api/types'
import { fmtNum, fmtSigned, fmtSignedPct, pnlClass } from '../../utils/format'

/** 小指标格：available / unrealised_pnl */
function MetricCell({ label, value, cls }: { label: string; value: string; cls?: string }) {
  return (
    <div className="rounded-lg border border-white/5 bg-white/[.03] p-2.5">
      <div className="text-[10px] text-zinc-500">{label}</div>
      <div className={`mt-0.5 font-mono text-sm tabular-nums ${cls ?? 'text-zinc-200'}`}>
        {value}
      </div>
    </div>
  )
}

export default function AccountPanel({
  account,
  mode,
  equityChangePct,
  dailyStats,
}: {
  account: AccountInfo | null
  /** 运行模式（标题展示用） */
  mode: string
  /** 权益曲线首末点涨跌幅（%）；undefined（空数据/首点为 0）不渲染涨跌行 */
  equityChangePct?: number
  /** 当日统计；null 时底部行整体降级不渲染 */
  dailyStats?: DailyStats | null
}) {
  return (
    <section className="space-y-4 rounded-xl border border-white/5 bg-zinc-900/60 p-4 backdrop-blur">
      <div className="flex items-center justify-between">
        <h3 className="text-xs tracking-widest text-zinc-500">
          账户 · {mode ? mode.toUpperCase() : '…'}
        </h3>
        <span className="font-mono text-[10px] text-zinc-600">USDT 本位</span>
      </div>
      {account === null ? (
        <p className="py-6 text-center text-sm text-zinc-500">加载中…</p>
      ) : (
        <>
          <div>
            <div className="mb-1 text-[11px] text-zinc-500">equity(账户权益)</div>
            <div className="font-mono text-3xl font-bold tabular-nums text-zinc-50">
              {fmtNum(account.equity)}
            </div>
            {equityChangePct !== undefined && (
              <div className={`mt-1 font-mono text-[11px] tabular-nums ${pnlClass(equityChangePct)}`}>
                {equityChangePct >= 0 ? '▲' : '▼'} {fmtSignedPct(equityChangePct / 100)} · 累计
              </div>
            )}
          </div>
          <div className="grid grid-cols-2 gap-3">
            <MetricCell label="available(可用余额)" value={fmtNum(account.available)} />
            <MetricCell
              label="unrealised_pnl(未实现盈亏)"
              value={fmtSigned(account.unrealised_pnl)}
              cls={pnlClass(account.unrealised_pnl)}
            />
          </div>
          {dailyStats && (
            <div className="flex justify-between border-t border-white/5 pt-3 text-[11px] text-zinc-500">
              <span>
                今日已实现{' '}
                <span className={`font-mono tabular-nums ${pnlClass(dailyStats.realized_pnl)}`}>
                  {fmtSigned(dailyStats.realized_pnl)}
                </span>
              </span>
              <span>
                当日开仓单{' '}
                <span className="font-mono tabular-nums text-zinc-300">
                  {dailyStats.orders_today}
                  <span className="text-zinc-600">/{dailyStats.max_orders_per_day}</span>
                </span>
              </span>
            </div>
          )}
        </>
      )}
    </section>
  )
}
