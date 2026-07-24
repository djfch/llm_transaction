/**
 * 权益小曲线（方案 C 换皮）：轻量 SVG sparkline，青色线 + 渐变面积，暗底。
 * 沿用旧权益图的数据防御（排序/去重/空态），但不引坐标轴，适配侧栏小尺寸。
 * 数据由父级装配层下发（哑组件）。
 */
import { useId, useMemo } from 'react'
import type { EquityPoint } from '../../api/types'
import { fmtNum, fmtSignedPct, fmtTime, pnlClass } from '../../utils/format'

// sparkline 画布基准（viewBox 等比缩放，preserveAspectRatio=none 充满容器）
const W = 280
const H = 84
const PAD = 4 // 上下留白，避免极值贴边

/** 归一化折线/面积路径；点数不足或全等值时退化为一字形 */
function buildPaths(values: number[]): { line: string; area: string } {
  const min = Math.min(...values)
  const max = Math.max(...values)
  const span = max - min || 1 // 全等值时按平线处理
  const n = values.length
  const x = (i: number) => (n > 1 ? (i / (n - 1)) * W : W / 2)
  const y = (v: number) => H - PAD - ((v - min) / span) * (H - PAD * 2)
  const pts = values.map((v, i) => `${x(i).toFixed(2)},${y(v).toFixed(2)}`)
  // 单点防御：画一条横线
  const line = n > 1 ? `M ${pts.join(' L ')}` : `M 0,${y(values[0]).toFixed(2)} L ${W},${y(values[0]).toFixed(2)}`
  return { line, area: `${line} L ${W},${H} L 0,${H} Z` }
}

export default function EquityMiniChart({
  points,
  equityChangePct,
}: {
  points: EquityPoint[]
  /** 以初始权益为基准的累计收益率（百分数）；由页面装配层统一计算。 */
  equityChangePct?: number
}) {
  const gradientId = useId()
  // 时间升序 + 防御非法点（时间不可解析/权益非有限数）
  const series = useMemo(
    () =>
      points
        .filter((p) => Number.isFinite(p.equity) && !Number.isNaN(new Date(p.time).getTime()))
        .sort((a, b) => new Date(a.time).getTime() - new Date(b.time).getTime()),
    [points],
  )
  const paths = useMemo(
    () => (series.length > 0 ? buildPaths(series.map((p) => p.equity)) : null),
    [series],
  )
  return (
    <section className="rounded-xl border border-white/5 bg-zinc-900/60 p-4 backdrop-blur">
      <div className="mb-2 flex items-center justify-between">
        <h3 className="text-xs tracking-widest text-zinc-500">权益曲线 equity</h3>
        {equityChangePct !== undefined && (
          <span className={`font-mono text-[11px] tabular-nums ${pnlClass(equityChangePct)}`}>
            {fmtSignedPct(equityChangePct / 100)}
          </span>
        )}
      </div>
      {paths === null ? (
        <p className="py-6 text-center text-sm text-zinc-500">暂无数据</p>
      ) : (
        <>
          <svg
            viewBox={`0 0 ${W} ${H}`}
            preserveAspectRatio="none"
            className="block h-[84px] w-full"
            data-testid="equity-mini-chart"
          >
            <defs>
              <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor="#22d3ee" stopOpacity="0.25" />
                <stop offset="100%" stopColor="#22d3ee" stopOpacity="0.02" />
              </linearGradient>
            </defs>
            <path d={paths.area} fill={`url(#${gradientId})`} stroke="none" />
            <path
              d={paths.line}
              fill="none"
              stroke="#22d3ee"
              strokeWidth="1.5"
              vectorEffect="non-scaling-stroke"
            />
          </svg>
          <div className="mt-1 flex justify-between font-mono text-[10px] tabular-nums text-zinc-600">
            <span>{fmtTime(series[0].time)}</span>
            <span>{`${fmtNum(series[0].equity)} → ${fmtNum(series[series.length - 1].equity)}`}</span>
            <span>{fmtTime(series[series.length - 1].time)}</span>
          </div>
        </>
      )}
    </section>
  )
}
