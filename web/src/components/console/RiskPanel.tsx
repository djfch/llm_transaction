/**
 * 硬性风控速览（方案 C 左下面板）：标题「硬性风控 · 代码保证」+ 四行风控参数 + 裁决说明。
 * 数据自管：api.getConfig() 取 risk 段；比例字段（0-1）×100 显示为百分比。
 * daily_loss_limit 语义（src/risk/rules.py）：当日已实现+未实现亏损超 daily_loss_limit×权益 时
 * 只平不开——同为权益比例，故与三个 pct 字段一样按百分比显示。
 */
import { api } from '../../api'
import { useApiData } from '../../hooks/useApiData'
import { fmtPct } from '../../utils/format'
import StateHint from '../StateHint'

/** 参数行：label(含字段名) 左灰字，值右等宽 */
function RiskRow({ label, value, cls }: { label: string; value: string; cls?: string }) {
  return (
    <li className="flex justify-between">
      <span className="text-zinc-500">{label}</span>
      <span className={`font-mono tabular-nums ${cls ?? 'text-zinc-300'}`}>{value}</span>
    </li>
  )
}

export default function RiskPanel() {
  const configQ = useApiData(() => api.getConfig(), [])
  const risk = configQ.data?.risk

  return (
    <section className="rounded-xl border border-white/5 bg-zinc-900/60 p-4 backdrop-blur">
      <h3 className="mb-3 text-xs tracking-widest text-zinc-500">硬性风控 · 代码保证</h3>
      <StateHint loading={configQ.loading} error={configQ.error}>
        {risk && (
          <>
            <ul className="space-y-2 text-[11px]">
              <RiskRow label="单仓上限 max_position_pct" value={fmtPct(risk.max_position_pct)} />
              <RiskRow
                label="总仓上限 max_total_position_pct"
                value={fmtPct(risk.max_total_position_pct)}
              />
              <RiskRow label="价格偏离 max_deviation" value={fmtPct(risk.max_deviation)} />
              <RiskRow
                label="日亏锁仓 daily_loss_limit"
                value={fmtPct(risk.daily_loss_limit)}
                cls="text-rose-300"
              />
            </ul>
            <p className="mt-3 text-[10px] leading-relaxed text-zinc-600">
              LLM 仅有建议权，所有订单经风控层裁决，违反约束直接拒绝（deny）。
            </p>
          </>
        )}
      </StateHint>
    </section>
  )
}
