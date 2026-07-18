/**
 * 配置中心：风控参数、常规设置（provider/model/通知）、白名单、system_prompt、密钥状态。
 */
import { api } from '../api'
import { useApiData } from '../hooks/useApiData'
import Card from '../components/Card'
import StateHint from '../components/StateHint'
import GeneralForm from './config/GeneralForm'
import RiskForm from './config/RiskForm'
import SecretsBadges from './config/SecretsBadges'
import StrategyEditor from './config/StrategyEditor'
import WatchlistEditor from './config/WatchlistEditor'

export default function ConfigPage() {
  const configQ = useApiData(() => api.getConfig(), [])
  const secretsQ = useApiData(() => api.getSecretsStatus(), [])
  const watchlistQ = useApiData(() => api.getWatchlist(), [])
  const strategyQ = useApiData(() => api.getStrategy(), [])

  return (
    <div className="space-y-6">
      <Card title="secrets(密钥配置状态)">
        <StateHint loading={secretsQ.loading} error={secretsQ.error}>
          {secretsQ.data && <SecretsBadges status={secretsQ.data} />}
        </StateHint>
      </Card>

      <Card title="常规设置 general（保存后下一轮决策生效）">
        <StateHint loading={configQ.loading} error={configQ.error}>
          {configQ.data && (
            <GeneralForm
              initial={configQ.data}
              onSave={async (next) => {
                await api.putConfig(next)
                configQ.reload()
              }}
            />
          )}
        </StateHint>
      </Card>

      <Card title="风控参数 risk">
        <StateHint loading={configQ.loading} error={configQ.error}>
          {configQ.data && (
            <RiskForm
              initial={configQ.data.risk}
              onSave={async (risk) => {
                await api.putConfig({ ...configQ.data!, risk })
                configQ.reload()
              }}
            />
          )}
        </StateHint>
      </Card>

      <Card title="watchlist(交易对白名单)">
        <StateHint loading={watchlistQ.loading} error={watchlistQ.error}>
          {watchlistQ.data && (
            <WatchlistEditor
              initial={watchlistQ.data}
              onSave={async (list) => {
                await api.putWatchlist(list)
                watchlistQ.reload()
              }}
            />
          )}
        </StateHint>
      </Card>

      <Card title="strategy(system_prompt.md 在线编辑)">
        <StateHint loading={strategyQ.loading} error={strategyQ.error}>
          {strategyQ.data !== null && (
            <StrategyEditor
              initial={strategyQ.data}
              onSave={async (content) => {
                await api.putStrategy(content)
                strategyQ.reload()
              }}
            />
          )}
        </StateHint>
      </Card>
    </div>
  )
}
