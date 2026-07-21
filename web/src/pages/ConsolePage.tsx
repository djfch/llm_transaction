/**
 * AI 大脑观察舱 · 单页装配（设计基准 design_proposals/scheme-c-agent.html）：
 * sticky TopBar → 首屏 12 列 grid（左 3：账户+权益曲线+硬性风控 / 中 6：实时决策轮主角 / 右 3：K线+持仓）
 * → 第二屏 决策时间线(8/12) + Agent 笔记(4/12) → 成交记录全宽；配置抽屉右侧滑入（含 paper 权益重置）。
 * 数据装配：status/account/positions/equity/当日统计 经 useApiData 注入面板 props；
 * WS round_start/round 事件联动刷新 account/positions/equity/当日统计（时间线与笔记各自管理分页）；
 * K线买卖点 / 成交行点击定位决策轮由 RoundFocusProvider 贯通。
 */
import { useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import type { AccountInfo, DailyStats, EquityPoint, Position } from '../api/types'
import AccountPanel from '../components/console/AccountPanel'
import ConfigDrawer from '../components/console/ConfigDrawer'
import EquityMiniChart from '../components/console/EquityMiniChart'
import KlinePanel from '../components/console/KlinePanel'
import LiveRoundHero from '../components/console/LiveRoundHero'
import NotesPanel from '../components/console/NotesPanel'
import PositionsPanel from '../components/console/PositionsPanel'
import RiskPanel from '../components/console/RiskPanel'
import RoundTimeline from '../components/console/RoundTimeline'
import TopBar from '../components/console/TopBar'
import TradesTable from '../components/console/TradesTable'
import { useApiData } from '../hooks/useApiData'
import { RoundFocusProvider } from '../hooks/useRoundFocus'
import { useWs } from '../hooks/useWs'

/** 权益曲线首末点涨跌幅（%）：空数组或首点为 0 → undefined（面板不渲染该行） */
function equityChangePctOf(points: EquityPoint[]): number | undefined {
  if (points.length === 0) return undefined
  const sorted = [...points].sort((a, b) => new Date(a.time).getTime() - new Date(b.time).getTime())
  const first = sorted[0].equity
  if (first === 0) return undefined
  return ((sorted[sorted.length - 1].equity - first) / first) * 100
}

/** 页面数据：状态、账户、持仓、权益与当日统计；笔记分页由 NotesPanel 独立管理。 */
function useConsoleData() {
  const status = useApiData(() => api.getStatus(), [])
  const account = useApiData(() => api.getAccount(), [])
  const positions = useApiData(() => api.getPositions(), [])
  const equity = useApiData(() => api.getEquity(), [])
  // 当日统计走后端 /api/daily_stats（风控口径）；失败时 data 为 null，账户面板底部行降级不渲染
  const daily = useApiData(() => api.getDailyStats(), [])
  const { connected, lastMessage } = useWs()
  const { reload: reloadAccount } = account
  const { reload: reloadPositions } = positions
  const { reload: reloadEquity } = equity
  const { reload: reloadDaily } = daily
  useEffect(() => {
    if (lastMessage?.type !== 'round_start' && lastMessage?.type !== 'round') return
    reloadAccount()
    reloadPositions()
    reloadEquity()
    reloadDaily() // 新轮成交改变当日已实现/开仓单口径
  }, [lastMessage, reloadAccount, reloadPositions, reloadEquity, reloadDaily])
  return { status, account, positions, equity, daily, connected }
}

/** 首屏：左栏账户+权益曲线+硬性风控 / 中央实时轮主角 / 右栏 K线+持仓（移动端实时轮优先） */
function FirstScreen({
  account,
  mode,
  points,
  equityChangePct,
  dailyStats,
  positions,
  onPositionsChanged,
}: {
  account: AccountInfo | null
  mode: string
  points: EquityPoint[]
  equityChangePct?: number
  dailyStats: DailyStats | null
  positions: Position[]
  onPositionsChanged: () => void
}) {
  return (
    <section className="grid grid-cols-12 gap-4 pt-5">
      <aside className="order-2 col-span-12 space-y-4 lg:order-1 lg:col-span-3">
        <AccountPanel account={account} mode={mode} equityChangePct={equityChangePct} dailyStats={dailyStats} />
        <EquityMiniChart points={points} />
        <RiskPanel />
      </aside>
      <div className="order-1 col-span-12 lg:order-2 lg:col-span-6">
        <LiveRoundHero />
      </div>
      <aside className="order-3 col-span-12 space-y-4 lg:col-span-3">
        <KlinePanel />
        <PositionsPanel positions={positions} onChanged={onPositionsChanged} />
      </aside>
    </section>
  )
}

/** 第二屏：决策时间线(8/12) + Agent 笔记(4/12)；第三屏：成交记录全宽。 */
function SecondScreen() {
  return (
    <>
      <section className="mt-6 grid grid-cols-12 gap-4">
        <div className="col-span-12 lg:col-span-8">
          <RoundTimeline />
        </div>
        <aside className="col-span-12 lg:col-span-4">
          <NotesPanel />
        </aside>
      </section>
      <section className="mt-8">
        <TradesTable />
      </section>
    </>
  )
}

/** 五路注入查询的失败横幅：任一失败列出失败数据源（哑组件只渲染空态，失败在此统一透出） */
function LoadErrorBanner({ errors }: { errors: Array<[string, string | null]> }) {
  const failed = errors.filter(([, e]) => e !== null)
  if (failed.length === 0) return null
  return (
    <div
      role="alert"
      className="mt-4 rounded-lg border border-rose-500/40 bg-rose-500/10 px-4 py-2.5 text-xs text-rose-300"
    >
      数据加载失败：{failed.map(([name]) => name).join('、')}
      （相关面板显示为空态/旧值，请检查后端服务后刷新重试）
    </div>
  )
}

export default function ConsolePage() {
  const { status, account, positions, equity, daily, connected } = useConsoleData()
  const [configOpen, setConfigOpen] = useState(false)
  // 权益曲线首末点涨跌幅（账户面板累计涨跌行；空数据/首点为 0 → undefined 不渲染）
  const equityChangePct = useMemo(() => equityChangePctOf(equity.data ?? []), [equity.data])

  // TopBar：agent 启停 / kill_switch 变更 → 刷状态 + 账户
  const onStatusChanged = () => {
    status.reload()
    account.reload()
  }
  // PositionsPanel：手动平仓 → 刷持仓 + 账户 + 权益曲线（成交表数据自管，不联动）
  const onPositionsChanged = () => {
    positions.reload()
    account.reload()
    equity.reload()
  }
  // 抽屉关闭 → 刷状态（保存密钥/LLM 配置后 TopBar 的 llm_configured 横幅需联动消失）
  const closeConfig = () => {
    setConfigOpen(false)
    status.reload()
  }
  // paper 权益重置成功 → 刷账户/持仓/权益/当日统计（重置改变 paper 端全部账面口径）
  const onPaperReset = () => {
    account.reload()
    positions.reload()
    equity.reload()
    daily.reload()
  }

  return (
    <RoundFocusProvider>
      <TopBar
        status={status.data}
        wsConnected={connected}
        onOpenConfig={() => setConfigOpen(true)}
        onChanged={onStatusChanged}
      />
      <main className="mx-auto max-w-[1440px] px-5 pb-16">
        <LoadErrorBanner
          errors={[
            ['状态', status.error],
            ['账户', account.error],
            ['持仓', positions.error],
            ['权益曲线', equity.error],
          ]}
        />
        <FirstScreen
          account={account.data}
          mode={status.data?.mode ?? ''}
          points={equity.data ?? []}
          equityChangePct={equityChangePct}
          dailyStats={daily.data}
          positions={positions.data ?? []}
          onPositionsChanged={onPositionsChanged}
        />
        <SecondScreen />
      </main>
      <ConfigDrawer open={configOpen} onClose={closeConfig} onReset={onPaperReset} />
    </RoundFocusProvider>
  )
}
