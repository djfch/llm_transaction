/**
 * 配置抽屉（方案 C 换皮）：右侧滑出，固定定位、宽 480px、滑入动画、遮罩点击 / ESC 关闭。
 * 内部完整复用 pages/config 五个表单（SecretsForm/GeneralForm/RiskForm/WatchlistEditor/StrategyEditor）
 * 及 StrategyVersions 版本历史；表单为 props 驱动，数据由本抽屉在打开时统一拉取。
 * 仅在 open 时挂载表单主体：关闭不发起请求，每次打开拉取最新配置。
 */
import { useEffect, useState, type ReactNode } from 'react'
import { api } from '../../api'
import { useApiData, type ApiQuery } from '../../hooks/useApiData'
import StateHint from '../StateHint'
import PaperEquitySetter from '../PaperEquitySetter'
import GeneralForm from '../../pages/config/GeneralForm'
import RiskForm from '../../pages/config/RiskForm'
import SecretsForm from '../../pages/config/SecretsForm'
import StrategyEditor from '../../pages/config/StrategyEditor'
import StrategyVersions from '../../pages/config/StrategyVersions'
import WatchlistEditor from '../../pages/config/WatchlistEditor'

/** 抽屉小节：标题 + 加载/失败/空态 + 表单内容 */
function DrawerSection<T>({
  title,
  query,
  children,
}: {
  title: string
  query: ApiQuery<T>
  children: (data: T) => ReactNode
}) {
  return (
    <section>
      <h3 className="mb-3 text-xs tracking-widest text-zinc-500">{title}</h3>
      {/* 仅初载（data 尚未就绪）转圈；后台 reload 保活 children——
          否则刷新会卸载表单，销毁成功提示/已保存标记等用户可见状态 */}
      <StateHint loading={query.loading && query.data === null} error={query.error}>
        {query.data !== null && children(query.data)}
      </StateHint>
    </section>
  )
}

/** 抽屉主体：打开时挂载，统一拉取四类配置数据并装配五个表单 */
function DrawerBody({ onReset }: { onReset: () => void }) {
  const configQ = useApiData(() => api.getConfig(), [])
  const secretsQ = useApiData(() => api.getSecretsStatus(), [])
  const watchlistQ = useApiData(() => api.getWatchlist(), [])
  const strategyQ = useApiData(() => api.getStrategy(), [])
  // PUT /api/config 响应附带的 LLM 热生效错误（空 = 正常）
  const [llmError, setLlmError] = useState<string | null>(null)
  // 策略保存成功信号：递增驱动 StrategyVersions 重拉版本列表（保存会生成新版本落库）
  const [strategySavedTick, setStrategySavedTick] = useState(0)

  return (
    <>
      <DrawerSection title="密钥状态 · 仅显示是否配置，永不回显明文" query={secretsQ}>
        {(status) => <SecretsForm status={status} onSaved={secretsQ.reload} />}
      </DrawerSection>

      <DrawerSection title="常规设置 · LLM 设置保存即生效" query={configQ}>
        {(config) => (
          <GeneralForm
            initial={config}
            onSave={async (next) => {
              const res = await api.putConfig(next)
              setLlmError(res.llm_error || null)
              configQ.reload()
            }}
          />
        )}
      </DrawerSection>
      {llmError && (
        <p
          role="alert"
          className="rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-300"
        >
          LLM 热生效错误：{llmError}
        </p>
      )}

      {/* 模拟盘权益重置（自主页账户面板挪入）：仅 paper 模式渲染；成功后经 onReset 联动刷新父级 */}
      {configQ.data?.mode === 'paper' && (
        <section>
          <h3 className="mb-3 text-xs tracking-widest text-zinc-500">模拟盘 paper · 权益重置</h3>
          <PaperEquitySetter onReset={onReset} />
        </section>
      )}

      <DrawerSection title="风控参数 · 代码强制，LLM 不可越权" query={configQ}>
        {(config) => (
          <RiskForm
            initial={config.risk}
            onSave={async (risk) => {
              await api.putConfig({ ...config, risk })
              configQ.reload()
            }}
          />
        )}
      </DrawerSection>

      <DrawerSection title="watchlist · 合约白名单" query={watchlistQ}>
        {(list) => (
          <WatchlistEditor
            initial={list}
            onSave={async (next) => {
              await api.putWatchlist(next)
              watchlistQ.reload()
            }}
          />
        )}
      </DrawerSection>

      <DrawerSection title="策略 Prompt · 在线编辑" query={strategyQ}>
        {(content) => (
          <>
            <StrategyEditor
              initial={content}
              onSave={async (next) => {
                await api.putStrategy(next)
                strategyQ.reload()
                setStrategySavedTick((t) => t + 1) // 保存生成新版本，通知版本历史重拉
              }}
            />
            {/* 版本历史侧栏（挂在编辑器下方）；回滚成功后经 strategyQ.reload 重拉全文，编辑器随 initial 同步；
                refreshKey 在保存成功后递增，驱动版本列表重拉 */}
            <StrategyVersions onRolledBack={strategyQ.reload} refreshKey={strategySavedTick} />
          </>
        )}
      </DrawerSection>
    </>
  )
}

export default function ConfigDrawer({
  open,
  onClose,
  onReset = () => {},
}: {
  open: boolean
  onClose: () => void
  /** paper 权益重置成功后的联动回调（父级负责刷新账户/持仓/权益/当日统计）；可选，缺省空操作 */
  onReset?: () => void
}) {
  // ESC 关闭（仅打开时监听）
  useEffect(() => {
    if (!open) return
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [open, onClose])

  // 滑入/滑出：visibility 随过渡离散翻转（关闭时动画结束才隐藏，避免场外可聚焦）
  const asideCls = open ? 'visible translate-x-0' : 'invisible translate-x-full'
  const overlayCls = open ? 'visible opacity-100' : 'invisible opacity-0'

  return (
    <>
      {/* 遮罩：点击关闭 */}
      <div
        aria-hidden="true"
        onClick={onClose}
        className={`fixed inset-0 z-40 bg-black/60 backdrop-blur-sm transition-[opacity,visibility] duration-300 ${overlayCls}`}
      />
      <aside
        role="dialog"
        aria-modal="true"
        aria-label="配置中心"
        aria-hidden={!open}
        className={`fixed right-0 top-0 z-50 flex h-full w-full flex-col border-l border-white/10 bg-zinc-950/95 backdrop-blur-xl transition-[transform,visibility] duration-300 sm:w-[480px] ${asideCls}`}
      >
        <div className="flex h-14 shrink-0 items-center gap-3 border-b border-white/5 px-5">
          <h2 className="font-bold text-zinc-100">配置中心</h2>
          <button
            type="button"
            aria-label="关闭配置中心"
            onClick={onClose}
            className="ml-auto text-lg leading-none text-zinc-500 transition hover:text-zinc-200"
          >
            ✕
          </button>
        </div>
        <div className="flex-1 space-y-6 overflow-y-auto p-5 text-sm">
          {open && <DrawerBody onReset={onReset} />}
        </div>
      </aside>
    </>
  )
}
