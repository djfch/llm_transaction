/**
 * 复盘报告面板：复盘结果列表（服务端分页，最新在前）+ 手动触发「立即复盘」。
 * 列表行：时间 / 复盘区间 / strategy_action 徽标（none→「未调整」，rewrite→「改策略 → vN」）；
 * error 非空的行以红字展示失败原因（该次复盘只落错误记录，不影响交易循环）。
 * 点击展开详情：lazy 拉取全文（列表 reportMd 截断 200 字符，详情给全文），
 * statsJson 解析出 总盈亏/胜率/盈亏比 小表格（字段缺失时降级不渲染该行）
 * + reportMd 全文（whitespace-pre-wrap 原样展示，不引 markdown 库）
 * + 关联审计轮的工具调用链（roundId 空串 = 功能上线前的老报告，灰字降级提示）。
 * 409（复盘进行中，按成功样式提示而非错误红）/ 503（LLM 未配置或未接线）经 ApiError.detail 提示。
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { api, ApiError } from '../../api'
import type { ReviewReport, ReviewReportSummary } from '../../api/types'
import { useApiData } from '../../hooks/useApiData'
import { usePageState } from '../../hooks/usePageState'
import { fmtNum, fmtPct2, fmtSigned, fmtTime, pnlClass } from '../../utils/format'
import StateHint from '../StateHint'
import PaginationControls from './PaginationControls'
import ReviewLiveStrip from './ReviewLiveStrip'
import ReviewToolChain from './ReviewToolChain'

/** 复盘报告每页条数（与决策时间线一致的交互口径）。 */
const PAGE_SIZE = 5
/** 空列表复用同一引用，避免 effect 依赖因新建空数组而反复变化。 */
const EMPTY_REPORTS: ReviewReportSummary[] = []

/** ISO 时间 → 本地日期（复盘区间按天展示）；不可解析时原样返回 */
function fmtDay(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleDateString('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit' })
}

interface StatRow {
  label: string
  value: string
  className?: string
}

/**
 * statsJson → 关键指标行（总盈亏/胜率/盈亏比）。
 * 口径由后端 stats.py 冻结（Decimal 序列化为字符串，分母为 0 时为 null）；
 * 解析失败或字段缺失/为 null 时跳过该行，绝不让统计表格影响报告正文渲染。
 */
function parseStatRows(statsJson: string): StatRow[] {
  let raw: unknown
  try {
    raw = JSON.parse(statsJson)
  } catch {
    return []
  }
  if (raw === null || typeof raw !== 'object') return []
  const fields = raw as Record<string, unknown>
  const num = (key: string): number | null => {
    const v = fields[key]
    if (typeof v !== 'string' && typeof v !== 'number') return null
    const n = Number(v)
    return Number.isFinite(n) ? n : null
  }
  const rows: StatRow[] = []
  const totalPnl = num('total_pnl')
  if (totalPnl !== null) rows.push({ label: '总盈亏', value: fmtSigned(totalPnl), className: pnlClass(totalPnl) })
  const winRate = num('win_rate')
  if (winRate !== null) rows.push({ label: '胜率', value: fmtPct2(winRate) })
  const profitFactor = num('profit_factor')
  if (profitFactor !== null) rows.push({ label: '盈亏比', value: fmtNum(profitFactor) })
  return rows
}

/** strategy_action 徽标：rewrite → 紫「改策略 → vN」；none → 灰「未调整」 */
function ActionBadge({ report }: { report: ReviewReportSummary }) {
  const base = 'rounded border px-2 py-0.5 text-[10px] font-medium'
  if (report.strategyAction === 'rewrite') {
    return (
      <span className={`${base} border-violet-400/40 bg-violet-400/10 text-violet-300`}>
        改策略 → v{report.newVersionId ?? '?'}
      </span>
    )
  }
  return <span className={`${base} border-zinc-600/50 bg-zinc-700/30 text-zinc-400`}>未调整</span>
}

/** 单条报告：摘要行 + 展开详情（lazy 拉取全文，卡片生命周期内缓存） */
function ReportItem({
  report,
  expanded,
  onToggle,
}: {
  report: ReviewReportSummary
  expanded: boolean
  onToggle: () => void
}) {
  const [detail, setDetail] = useState<ReviewReport | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // 展开时 lazy 拉取全文；loading 不进 deps（同 TimelineCard：避免清理函数自我取消），fetchedRef 防重入
  // retryTick：请求完成但结果被收起丢弃（alive=false）时自增，重跑 effect 重新请求（已展开时）
  const fetchedRef = useRef(false)
  const [retryTick, setRetryTick] = useState(0)
  useEffect(() => {
    if (!expanded || fetchedRef.current) return
    fetchedRef.current = true
    let alive = true
    setLoading(true)
    setError(null)
    api
      .getReviewReport(report.id)
      .then((d) => {
        if (alive) setDetail(d)
      })
      .catch((e: unknown) => {
        if (alive) {
          setError(e instanceof Error ? e.message : String(e))
          fetchedRef.current = false // 失败允许收起后再展开重试
        }
      })
      .finally(() => {
        if (alive) {
          setLoading(false)
          return
        }
        // 请求完成但结果被丢弃（收起时 cleanup 置 alive=false）：重置防重入并重跑 effect，
        // 已重新展开则重新请求，仍收起则留给下次展开
        fetchedRef.current = false
        setRetryTick((t) => t + 1)
      })
    return () => {
      alive = false
    }
  }, [expanded, report.id, retryTick])

  const statRows = detail === null ? [] : parseStatRows(detail.statsJson)

  return (
    <article className="rounded-xl border border-zinc-800 bg-zinc-900/60 p-4">
      <button type="button" onClick={onToggle} aria-expanded={expanded} className="block w-full text-left">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-mono text-[11px] tabular-nums text-zinc-500">{fmtTime(report.time)}</span>
          <span className="rounded border border-zinc-700/60 bg-zinc-800/40 px-2 py-0.5 text-[10px] text-zinc-400">
            区间 {fmtDay(report.periodStart)} ~ {fmtDay(report.periodEnd)}
          </span>
          <ActionBadge report={report} />
          <span className={`ml-auto text-[10px] text-zinc-600 transition-transform ${expanded ? 'rotate-90' : ''}`}>
            ▸
          </span>
        </div>
        {report.error !== '' ? (
          <p className="mt-2 text-xs text-rose-400">复盘失败：{report.error}</p>
        ) : (
          report.reportMd !== '' && (
            <p className="mt-2 line-clamp-2 text-[13px] leading-6 text-zinc-400">{report.reportMd}</p>
          )
        )}
      </button>

      {expanded && (
        <div className="mt-3 border-t border-zinc-800/80 pt-3">
          {loading && <p className="py-3 text-xs text-zinc-500">报告全文加载中…</p>}
          {error && <p className="py-3 text-xs text-rose-400">加载失败：{error}</p>}
          {detail && (
            <>
              {statRows.length > 0 && (
                <dl className="grid grid-cols-3 gap-2">
                  {statRows.map((row) => (
                    <div key={row.label} className="rounded-lg border border-zinc-800 bg-zinc-950/60 px-3 py-2">
                      <dt className="text-[10px] text-zinc-500">{row.label}</dt>
                      <dd className={`mt-0.5 font-mono text-sm tabular-nums ${row.className ?? 'text-zinc-200'}`}>
                        {row.value}
                      </dd>
                    </div>
                  ))}
                </dl>
              )}
              {detail.reportMd !== '' && (
                <div className="mt-3 whitespace-pre-wrap rounded-lg border border-zinc-800 bg-zinc-950/60 p-3 text-[13px] leading-6 text-zinc-300">
                  {detail.reportMd}
                </div>
              )}
              {/* 工具调用链：roundId 非空时内嵌该轮复盘审计详情；空串 = 老报告，灰字降级 */}
              {detail.roundId !== '' ? (
                <ReviewToolChain roundId={detail.roundId} />
              ) : (
                <p className="mt-3 text-xs text-zinc-600">该报告早于工具链留痕功能，无工具调用记录</p>
              )}
            </>
          )}
        </div>
      )}
    </article>
  )
}

/** 复盘报告面板：列表分页自管；「立即复盘」点火即返回，新报告落库后经状态条 onFinished 回第一页刷新。 */
export default function ReviewPanel() {
  const [total, setTotal] = useState(0)
  const [expandedId, setExpandedId] = useState<number | null>(null)
  const [running, setRunning] = useState(false)
  const [notice, setNotice] = useState<{ ok: boolean; text: string } | null>(null)
  const { page, totalPages, goToPage } = usePageState(total, PAGE_SIZE)
  const query = useApiData(() => api.getReviewReports(page * PAGE_SIZE, PAGE_SIZE), [page])
  const items = query.data?.items ?? EMPTY_REPORTS
  const { reload } = query

  useEffect(() => {
    if (query.data) setTotal(query.data.total)
  }, [query.data])

  /** 切换页面时收起旧页展开项，避免详情状态误带到新页。 */
  const changePage = useCallback(
    (nextPage: number) => {
      if (nextPage === page) return
      setExpandedId(null)
      goToPage(nextPage)
    },
    [goToPage, page],
  )

  /** 新报告出现在最前：收起展开项，回第一页（deps 驱动刷新）；已在第一页则原地刷新。轮结束顺手清掉「已启动」提示，避免绿条滞留。 */
  const refreshToLatest = useCallback(() => {
    setNotice(null)
    setExpandedId(null)
    if (page !== 0 && totalPages > 0) goToPage(0)
    else reload()
  }, [goToPage, page, reload, totalPages])

  /** 手动触发复盘：点火即返回，按钮立即恢复；进度经下方状态条呈现，成败报告落库后经 onFinished 刷新列表。
   *  点火成功广播 review-round-ignite：状态条不依赖 WS 也能激活（覆盖 WS 断线窗口内点火）。
   *  409（进行中）按成功样式提示而非错误红，并广播 review-round-catchup：任务由他处（别的标签页/自动调度）
   *  点火而本页 WS 断线时，状态条经补漏找回进行中轮；503 经 ApiError.detail 红字提示。 */
  const runNow = async () => {
    setRunning(true)
    setNotice(null)
    try {
      await api.runReview()
      setNotice({ ok: true, text: '复盘已启动，进度见下方状态条' })
      window.dispatchEvent(new CustomEvent('review-round-ignite'))
    } catch (e) {
      // 409 = 已有复盘在进行中：是状态提示而非失败，避免红色误导；顺手让状态条补漏激活
      if (e instanceof ApiError && e.status === 409) {
        setNotice({ ok: true, text: e.detail })
        window.dispatchEvent(new CustomEvent('review-round-catchup'))
      } else setNotice({ ok: false, text: e instanceof Error ? e.message : String(e) })
    } finally {
      setRunning(false)
    }
  }

  return (
    <section className="rounded-xl border border-zinc-800 bg-zinc-950/80 p-5 shadow-lg shadow-black/30">
      <header className="mb-4 flex flex-wrap items-center gap-2">
        <h2 className="text-sm font-semibold text-zinc-200">
          复盘报告 <span className="text-xs font-normal text-zinc-500">— 复盘结论与策略自进化留痕</span>
        </h2>
        <span className="ml-auto text-xs tabular-nums text-zinc-500">共 {total} 条复盘</span>
        <button
          type="button"
          disabled={running}
          onClick={runNow}
          className="rounded-lg border border-violet-400/50 bg-violet-400/10 px-3 py-1.5 text-xs text-violet-300 transition hover:bg-violet-400/20 disabled:opacity-40"
        >
          {running ? '复盘中…' : '立即复盘'}
        </button>
      </header>

      <ReviewLiveStrip onFinished={refreshToLatest} />

      {notice && (
        <div
          role="alert"
          className={`mb-3 flex items-center gap-2 rounded-lg border px-3 py-2 text-xs ${
            notice.ok
              ? 'border-emerald-400/40 bg-emerald-400/10 text-emerald-300'
              : 'border-rose-500/40 bg-rose-500/10 text-rose-300'
          }`}
        >
          {notice.text}
          <button
            type="button"
            onClick={() => setNotice(null)}
            className="ml-auto opacity-70 transition hover:opacity-100"
          >
            ✕
          </button>
        </div>
      )}

      <StateHint loading={query.loading} error={query.error} empty={total === 0}>
        <ol className="space-y-3">
          {items.map((report) => (
            <li key={report.id}>
              <ReportItem
                report={report}
                expanded={expandedId === report.id}
                onToggle={() => setExpandedId((current) => (current === report.id ? null : report.id))}
              />
            </li>
          ))}
        </ol>
        <PaginationControls
          page={page}
          total={total}
          pageSize={PAGE_SIZE}
          itemLabel="复盘"
          loading={query.loading}
          onPageChange={changePage}
        />
      </StateHint>
    </section>
  )
}
