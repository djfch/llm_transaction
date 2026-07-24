/**
 * AI 大脑观察舱 · 单页装配（设计基准 design_proposals/scheme-c-agent.html）：
 * sticky TopBar → 首屏 12 列 grid（左 3：账户+权益曲线+硬性风控 / 中 6：实时决策轮主角 / 右 3：K线+持仓）
 * → 第二屏 决策时间线(8/12) + Agent 笔记(4/12) → 成交记录全宽；配置抽屉右侧滑入（含 paper 权益重置）。
 * 数据装配：status/account/positions/openOrders/equity/daily 六路查询经 useApiData 注入面板 props；
 * WS round_start/round 事件联动刷新账户、持仓、挂单、权益与当日统计；时间线与笔记各自管理分页；
 * K线买卖点 / 成交行点击定位决策轮由 RoundFocusProvider 贯通。
 */
import { useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import type { AccountInfo, DailyStats, EquityPoint, OpenOrder, Position } from '../api/types'
import AccountPanel from '../components/console/AccountPanel'
import ConfigDrawer from '../components/console/ConfigDrawer'
import EquityMiniChart from '../components/console/EquityMiniChart'
import { equityChangePct, withCurrentEquity } from '../components/console/equityPresentation'
import KlinePanel from '../components/console/KlinePanel'
import LiveRoundHero from '../components/console/LiveRoundHero'
import NotesPanel from '../components/console/NotesPanel'
import PositionsPanel from '../components/console/PositionsPanel'
import OpenOrdersPanel from '../components/console/OpenOrdersPanel'
import RiskPanel from '../components/console/RiskPanel'
import RoundTimeline from '../components/console/RoundTimeline'
import TopBar from '../components/console/TopBar'
import TradesTable from '../components/console/TradesTable'
import { useApiData } from '../hooks/useApiData'
import { useLivePortfolio } from '../hooks/useLivePortfolio'
import { RoundFocusProvider } from '../hooks/useRoundFocus'

/** 页面数据：状态、账户、持仓、挂单、权益与当日统计；笔记分页由 NotesPanel 独立管理。 */
function useConsoleData() {
  const status = useApiData(() => api.getStatus(), [])
  const portfolio = useLivePortfolio()
  const openOrders = useApiData(() => api.getOpenOrders(), [])
  const equity = useApiData(() => api.getEquity(), [])
  // 当日统计走后端 /api/daily_stats（风控口径）；失败时 data 为 null，账户面板底部行降级不渲染
  const daily = useApiData(() => api.getDailyStats(), [])
  const { connected, lastMessage } = portfolio
  const { reload: reloadOpenOrders } = openOrders
  const { reload: reloadEquity } = equity
  const { reload: reloadDaily } = daily
  useEffect(() => {
    if (lastMessage?.type !== 'round_start' && lastMessage?.type !== 'round') return
    reloadOpenOrders()
    reloadEquity()
    reloadDaily() // 新轮成交改变当日已实现/开仓单口径
  }, [lastMessage, reloadOpenOrders, reloadEquity, reloadDaily])
  return { status, portfolio, openOrders, equity, daily, connected }
}

/** 首屏：右栏将当前持仓与未成交挂单相邻展示，便于一起核对和操作。 */
function FirstScreen({
  account,
  mode,
  points,
  equityChangePct,
  dailyStats,
  positions,
  openOrders,
  onPositionsChanged,
  onOpenOrdersChanged,
}: {
  account: AccountInfo | null
  mode: string
  points: EquityPoint[]
  equityChangePct?: number
  dailyStats: DailyStats | null
  positions: Position[]
  openOrders: OpenOrder[]
  onPositionsChanged: () => void
  onOpenOrdersChanged: () => void
}) {
  return (
    <section className="grid grid-cols-12 gap-4 pt-5">
      <aside className="order-2 col-span-12 space-y-4 lg:order-1 lg:col-span-3">
        <AccountPanel account={account} mode={mode} equityChangePct={equityChangePct} dailyStats={dailyStats} />
        <EquityMiniChart points={points} equityChangePct={equityChangePct} />
        <RiskPanel />
      </aside>
      <div className="order-1 col-span-12 lg:order-2 lg:col-span-6">
        <LiveRoundHero />
      </div>
      <aside className="order-3 col-span-12 space-y-4 lg:col-span-3">
        <KlinePanel />
        <PositionsPanel positions={positions} onChanged={onPositionsChanged} />
        <OpenOrdersPanel orders={openOrders} onChanged={onOpenOrdersChanged} />
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

/** 六路关键查询的失败横幅；daily 当日统计独立降级，不进入该横幅。 */
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
  const { status, portfolio, openOrders, equity, daily, connected } = useConsoleData()
  const [configOpen, setConfigOpen] = useState(false)
  const account = portfolio.data?.account ?? null
  const positions = portfolio.data?.positions ?? []
  const points = useMemo(() => {
    const history = equity.data?.points ?? []
    if (portfolio.data === null) return history
    return withCurrentEquity(history, portfolio.data.asOf, portfolio.data.account.equity)
  }, [equity.data, portfolio.data])
  const changePct = useMemo(() => {
    if (account === null || equity.data === null) return undefined
    return equityChangePct(account.equity, equity.data.initialEquity)
  }, [account, equity.data])

  // TopBar：agent 启停 / kill_switch 变更 → 刷状态 + 账户
  const onStatusChanged = () => {
    status.reload()
    portfolio.reloadImmediately()
  }
  // PositionsPanel：手动平仓 → 刷持仓 + 账户 + 权益曲线（成交表数据自管，不联动）
  const onPositionsChanged = () => {
    portfolio.reloadImmediately()
    equity.reload()
  }
  // 撤单不会改变持仓，但会释放可用余额，因此只刷新挂单和账户。
  const onOpenOrdersChanged = () => {
    openOrders.reload()
    portfolio.reloadImmediately()
  }
  // 抽屉关闭 → 刷状态（保存密钥/LLM 配置后 TopBar 的 llm_configured 横幅需联动消失）
  const closeConfig = () => {
    setConfigOpen(false)
    status.reload()
  }
  // paper 权益重置成功 → 刷账户/持仓/权益/当日统计（重置改变 paper 端全部账面口径）
  const onPaperReset = () => {
    portfolio.reloadImmediately()
    openOrders.reload()
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
            ['未成交挂单', openOrders.error],
            ['状态', status.error],
            ['组合快照', portfolio.error],
            ['权益曲线', equity.error],
          ]}
        />
        <FirstScreen
          account={account}
          mode={status.data?.mode ?? ''}
          points={points}
          equityChangePct={changePct}
          dailyStats={daily.data}
          positions={positions}
          openOrders={openOrders.data ?? []}
          onPositionsChanged={onPositionsChanged}
          onOpenOrdersChanged={onOpenOrdersChanged}
        />
        <SecondScreen />
      </main>
      <ConfigDrawer open={configOpen} onClose={closeConfig} onReset={onPaperReset} />
    </RoundFocusProvider>
  )
}
