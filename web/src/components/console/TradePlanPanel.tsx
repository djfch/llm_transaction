/**
 * 交易计划面板（首屏左栏，只读）：展示 agent 当前维护的全局唯一一份交易计划（GET /api/plan）。
 * 计划只能由 agent 的 update_trade_plan / clear_trade_plan 工具维护，界面不提供任何编辑入口。
 * 刷新语义：refreshKey 变化（WS 决策轮事件，由 ConsolePage 驱动）时重拉——agent 更新计划就发生在决策轮里。
 * 空态（content 空串）显示「暂无交易计划」；后台重拉期间保留旧全文，不闪烁「加载中…」。
 */
import { api } from '../../api'
import { useApiData } from '../../hooks/useApiData'
import { fmtTime } from '../../utils/format'
import StateHint from '../StateHint'

export default function TradePlanPanel({ refreshKey }: { refreshKey: number }) {
  const planQ = useApiData(() => api.getPlan(), [refreshKey])
  const plan = planQ.data
  const empty = plan !== null && plan.content === ''

  return (
    <section className="rounded-xl border border-white/5 bg-zinc-900/60 p-4 backdrop-blur">
      <header className="mb-3 flex items-center gap-2">
        <h3 className="text-xs tracking-widest text-zinc-500">交易计划 · trade_plan</h3>
        {plan !== null && plan.content !== '' && plan.updatedAt !== null && (
          <span className="ml-auto font-mono text-[10px] tabular-nums text-zinc-500">
            更新于 {fmtTime(plan.updatedAt)}
          </span>
        )}
      </header>

      {/* loading 门控 data === null：后台重拉（WS 决策轮）期间保留旧全文，不闪烁（同 StrategyPanel 保活先例） */}
      <StateHint loading={planQ.loading && plan === null} error={planQ.error} empty={empty}>
        <pre className="max-h-80 overflow-auto whitespace-pre-wrap break-words rounded-lg border border-zinc-800 bg-zinc-950 p-3 text-[12px] leading-6 text-zinc-300">
          {plan?.content}
        </pre>
      </StateHint>
    </section>
  )
}
