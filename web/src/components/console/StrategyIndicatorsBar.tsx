/**
 * 当前策略指标徽标条（只读）：短名单展示名（EMA20 · EMA50 · …）+ scalar 指标当前值徽标
 * （如「持仓量 123,456」「ATR14 892.54」）。
 * 数据由 KlinePanel 的 useIndicatorSeries 统一拉取后透传，本组件不自行请求；
 * 展示名由 utils/indicatorSeries.displayName 处理（oi → 持仓量，其余取英文标识段）。
 */
import type { ScalarBadge } from '../../utils/indicatorSeries'

export default function StrategyIndicatorsBar({
  names,
  badges,
  error,
}: {
  names: string[] // 短名单展示名（已按后端顺序）
  badges: ScalarBadge[] // scalar 指标徽标（label + 已格式化 text）
  error: string | null
}) {
  if (error) {
    return (
      <div data-testid="strategy-indicators-bar" className="mt-2 text-[11px] text-rose-400">
        指标加载失败：{error}
      </div>
    )
  }
  if (names.length === 0) return null
  return (
    <div
      data-testid="strategy-indicators-bar"
      className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-zinc-500"
    >
      <span>
        当前策略指标：<span className="font-mono text-zinc-300">{names.join(' · ')}</span>
      </span>
      {badges.map((b) => (
        <span
          key={b.key}
          className="rounded border border-zinc-700/60 bg-zinc-900/70 px-1.5 py-0.5 font-mono text-[10px] text-zinc-400"
        >
          {b.label} <span className="tabular-nums text-cyan-300">{b.text}</span>
        </span>
      ))}
    </div>
  )
}
