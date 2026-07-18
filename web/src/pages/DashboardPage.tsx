/**
 * 仪表盘：权益曲线、K 线、账户概览、持仓卡片、运行状态（含 kill_switch 与 agent 启停）、最近笔记。
 * WS 推送 position/trade/round 时自动刷新对应数据。
 */
import { useEffect } from 'react'
import { api } from '../api'
import { useApiData } from '../hooks/useApiData'
import { useWs } from '../hooks/useWs'
import CandleCard from '../components/CandleCard'
import Card from '../components/Card'
import EquityChart from '../components/EquityChart'
import MetricCard from '../components/MetricCard'
import PaperEquitySetter from '../components/PaperEquitySetter'
import PositionCard from '../components/PositionCard'
import StateHint from '../components/StateHint'
import StatusCard from '../components/StatusCard'
import { fmtNum, fmtSigned, fmtTime, pnlClass } from '../utils/format'

export default function DashboardPage() {
  const statusQ = useApiData(() => api.getStatus(), [])
  const accountQ = useApiData(() => api.getAccount(), [])
  const positionsQ = useApiData(() => api.getPositions(), [])
  const equityQ = useApiData(() => api.getEquity(), [])
  const notesQ = useApiData(() => api.getNotes(), [])
  const { lastMessage } = useWs()

  // WS 推送驱动增量刷新；round(决策轮)可能伴随自动成交，持仓/账户/曲线一并刷新
  useEffect(() => {
    if (!lastMessage) return
    if (lastMessage.type === 'position' || lastMessage.type === 'trade') {
      positionsQ.reload()
      accountQ.reload()
    }
    if (lastMessage.type === 'round') {
      statusQ.reload()
      positionsQ.reload()
      accountQ.reload()
      equityQ.reload()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- 只跟随消息变化
  }, [lastMessage])

  const status = statusQ.data
  const account = accountQ.data
  // 平仓/设置金额后刷新持仓、账户与权益曲线
  const refreshAccount = () => {
    positionsQ.reload()
    accountQ.reload()
    equityQ.reload()
  }

  return (
    <div className="space-y-6">
      {/* 账户概览（paper 模式下可设置金额） */}
      <StateHint loading={accountQ.loading} error={accountQ.error}>
        {account && (
          <div className="space-y-4">
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
              <MetricCard label="equity(账户权益 USDT)" value={fmtNum(account.equity)} />
              <MetricCard label="available(可用余额 USDT)" value={fmtNum(account.available)} />
              <MetricCard
                label="unrealised_pnl(未实现盈亏 USDT)"
                value={fmtSigned(account.unrealised_pnl)}
                tone={account.unrealised_pnl > 0 ? 'up' : account.unrealised_pnl < 0 ? 'down' : 'default'}
              />
            </div>
            {status?.mode === 'paper' && <PaperEquitySetter onReset={refreshAccount} />}
          </div>
        )}
      </StateHint>

      {/* 权益曲线 */}
      <Card title="权益曲线 equity">
        <StateHint loading={equityQ.loading} error={equityQ.error}>
          {equityQ.data && <EquityChart data={equityQ.data} />}
        </StateHint>
      </Card>

      {/* K 线（合约/周期可切换） */}
      <CandleCard />

      {/* 持仓 + 运行状态 */}
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
        <div className="space-y-4 lg:col-span-2">
          <h2 className="text-sm font-semibold text-slate-300">当前持仓 positions</h2>
          <StateHint
            loading={positionsQ.loading}
            error={positionsQ.error}
            empty={(positionsQ.data ?? []).length === 0}
          >
            <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
              {(positionsQ.data ?? []).map((p) => (
                <PositionCard key={p.contract} position={p} onClosed={refreshAccount} />
              ))}
            </div>
          </StateHint>
        </div>

        <div className="space-y-4">
          <StatusCard
            status={status}
            loading={statusQ.loading}
            error={statusQ.error}
            onChanged={statusQ.reload}
          />
        </div>
      </div>

      {/* 最近笔记 */}
      <Card title="最近笔记 notes">
        <StateHint loading={notesQ.loading} error={notesQ.error} empty={(notesQ.data ?? []).length === 0}>
          <ul className="space-y-3">
            {(notesQ.data ?? []).slice(0, 5).map((n, i) => (
              <li key={i} className="text-sm">
                <span className="mr-3 text-xs tabular-nums text-slate-500">{fmtTime(n.time)}</span>
                <span className="text-slate-300">{n.content}</span>
              </li>
            ))}
          </ul>
        </StateHint>
      </Card>

      {/* 底部账户汇总条（盈亏着色示例） */}
      {account && (
        <p className="text-right text-xs text-slate-500">
          数据更新时间 {fmtTime(new Date().toISOString())} · 未实现盈亏{' '}
          <span className={pnlClass(account.unrealised_pnl)}>{fmtSigned(account.unrealised_pnl)}</span>
        </p>
      )}
    </div>
  )
}
