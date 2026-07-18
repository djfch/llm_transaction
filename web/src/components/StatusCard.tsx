import { api } from '../api'
import type { StatusInfo } from '../api/types'
import AgentControl from './AgentControl'
import Badge from './Badge'
import Card from './Card'
import KillSwitchButton from './KillSwitchButton'
import StateHint from './StateHint'
import { fmtUptime } from '../utils/format'

/** 运行状态卡：status 全字段 + kill_switch 开关 + agent(交易代理) 启停 */
export default function StatusCard({
  status,
  loading,
  error,
  onChanged,
}: {
  status: StatusInfo | null
  loading: boolean
  error: string | null
  /** kill_switch / agent 状态变更后的刷新回调 */
  onChanged: () => void
}) {
  return (
    <Card title="运行状态 status">
      <StateHint loading={loading} error={error}>
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
              <dt className="text-slate-500">agent_running(运行状态)</dt>
              <dd>
                <Badge text={status.agent_running ? '运行中' : '已停止'} tone={status.agent_running ? 'ok' : 'neutral'} />
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
              <dt className="text-slate-500">llm_configured(LLM配置)</dt>
              <dd>
                <Badge
                  text={status.llm_configured ? '已配置' : '未配置'}
                  tone={status.llm_configured ? 'ok' : 'warn'}
                />
              </dd>
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
          disabled={status === null}
          onToggle={async (next) => {
            await api.setKillSwitch(next)
            onChanged()
          }}
        />
        <p className="mt-2 text-xs text-slate-500">
          开启后风控拒绝一切新开仓，仅允许平仓；需点击两次确认。
        </p>
      </div>
      <div className="mt-3 border-t border-slate-800 pt-4">
        <AgentControl
          running={status?.agent_running ?? false}
          disabled={status === null}
          onToggle={async (next) => {
            if (next) await api.startAgent()
            else await api.stopAgent()
            onChanged()
          }}
        />
        <p className="mt-2 text-xs text-slate-500">停止后不再自动决策与下单；启动单击生效，停止需两次确认。</p>
      </div>
    </Card>
  )
}
