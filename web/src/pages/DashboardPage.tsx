/**
 * 仪表盘：权益曲线、账户概览、持仓卡片、运行状态、kill_switch、最近笔记。
 * WS 推送 position/trade/round 时自动刷新对应数据。
 */
import { useEffect } from 'react'
import { api } from '../api'
import { useApiData } from '../hooks/useApiData'
import { useWs } from '../hooks/useWs'
import Badge from '../components/Badge'
import Card from '../components/Card'
import EquityChart from '../components/EquityChart'
import KillSwitchButton from '../components/KillSwitchButton'
import MetricCard from '../components/MetricCard'
import PositionCard from '../components/PositionCard'
import StateHint from '../components/StateHint'
import { fmtNum, fmtSigned, fmtTime, fmtUptime, pnlClass } from '../utils/format'

export default function DashboardPage() {
  const statusQ = useApiData(() => api.getStatus(), [])
  const accountQ = useApiData(() => api.getAccount(), [])
  const positionsQ = useApiData(() => api.getPositions(), [])
  const equityQ = useApiData(() => api.getEquity(), [])
  const notesQ = useApiData(() => api.getNotes(), [])
  const { lastMessage } = useWs()

  // WS 推送驱动增量刷新
  useEffect(() => {
    if (!lastMessage) return
    if (lastMessage.type === 'position' || lastMessage.type === 'trade') {
      positionsQ.reload()
      accountQ.reload()
    }
    if (lastMessage.type === 'round') statusQ.reload()
    // eslint-disable-next-line react-hooks/exhaustive-deps -- 只跟随消息变化
  }, [lastMessage])

  const status = statusQ.data
  const account = accountQ.data

  return (
    <div className="space-y-6">
      {/* 账户概览 */}
      <StateHint loading={accountQ.loading} error={accountQ.error}>
        {account && (
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
            <MetricCard label="equity(账户权益 USDT)" value={fmtNum(account.equity)} />
            <MetricCard label="available(可用余额 USDT)" value={fmtNum(account.available)} />
            <MetricCard
              label="unrealised_pnl(未实现盈亏 USDT)"
              value={fmtSigned(account.unrealised_pnl)}
              tone={account.unrealised_pnl > 0 ? 'up' : account.unrealised_pnl < 0 ? 'down' : 'default'}
            />
          </div>
        )}
      </StateHint>

      {/* 权益曲线 */}
      <Card title="权益曲线 equity">
        <StateHint loading={equityQ.loading} error={equityQ.error}>
          {equityQ.data && <EquityChart data={equityQ.data} />}
        </StateHint>
      </Card>

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
                <PositionCard key={p.contract} position={p} />
              ))}
            </div>
          </StateHint>
        </div>

        <div className="space-y-4">
          <Card title="运行状态 status">
            <StateHint loading={statusQ.loading} error={statusQ.error}>
              {status && (
                <dl className="space-y-3 text-sm">
                  <div className="flex items-center justify-between">
                    <dt className="text-slate-500">mode(运行模式)</dt>
                    <dd>
                      <Badge
                        text={status.mode}
                        tone={status.mode === 'live' ? 'danger' : status.mode === 'testnet' ? 'warn' : 'info'}
                      />
                    </dd>
                  </div>
                  <div className="flex items-center justify-between">
                    <dt className="text-slate-500">uptime(运行时长)</dt>
                    <dd className="tabular-nums">{fmtUptime(status.uptime_seconds)}</dd>
                  </div>
                  <div className="flex items-center justify-between">
                    <dt className="text-slate-500">llm_provider(LLM 提供商)</dt>
                    <dd>{status.llm_provider}</dd>
                  </div>
                  <div className="flex items-center justify-between">
                    <dt className="text-slate-500">llm_model(模型)</dt>
                    <dd className="text-xs">{status.llm_model}</dd>
                  </div>
                  <div className="flex items-center justify-between">
                    <dt className="text-slate-500">kill_switch(紧急停止)</dt>
                    <dd>
                      <Badge
                        text={status.kill_switch ? '已触发' : '未触发'}
                        tone={status.kill_switch ? 'danger' : 'ok'}
                      />
                    </dd>
                  </div>
                </dl>
              )}
            </StateHint>
            <div className="mt-4 border-t border-slate-800 pt-4">
              <KillSwitchButton
                enabled={status?.kill_switch ?? false}
                onToggle={async (next) => {
                  await api.setKillSwitch(next)
                  statusQ.reload()
                }}
              />
              <p className="mt-2 text-xs text-slate-500">
                开启后风控拒绝一切新开仓，仅允许平仓；需点击两次确认。
              </p>
            </div>
          </Card>
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
