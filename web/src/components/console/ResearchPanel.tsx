/**
 * 研报面板：宏观与消息面前瞻研报列表（服务端分页，最新在前）+ 手动触发「生成研报」。
 * 列表行：时间 / 类型徽标（manual→手动、asia_open→亚盘、europe_open→欧盘、us_open→美盘）/
 * 方向徽标（偏多=绿 / 偏空=红 / 中性=灰）/ 置信度徽标（高=蓝 / 中=黄 / 低=灰）；
 * error 非空的行以红字展示失败原因（该次研报只落错误记录，不影响交易循环）。
 * 点击展开详情：lazy 拉取全文（列表 narrative 截断 200 字符，详情给全文），
 * 依次渲染 结论条（方向+置信度+前瞻窗口）→ narrative 全文 → 证据/风险列表（无数据整块不渲染）
 * → 因果链（chip 节点链 + 状态/置信度 + timeline 溯源标注）→ 关联审计轮工具调用链
 * （roundId 空串 = 无工具调用记录，灰字降级提示）。
 * 409（研报生成中）/ 503（LLM 未配置或未接线）/ 422（hours 越界）经 ApiError.detail 提示。
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../../api'
import type { CausalLinkView, ChainNode, ResearchReportDetail, ResearchReportSummary } from '../../api/types'
import { useApiData } from '../../hooks/useApiData'
import { usePageState } from '../../hooks/usePageState'
import { fmtTime } from '../../utils/format'
import StateHint from '../StateHint'
import PaginationControls from './PaginationControls'
import { ResearchAssetBadges, ResearchAssetDetails } from './ResearchAssetViews'
import ResearchLiveStrip from './ResearchLiveStrip'
import ReviewToolChain from './ReviewToolChain'

/** 研报每页条数（与复盘报告一致的交互口径）。 */
const PAGE_SIZE = 5
/** 空列表复用同一引用，避免 effect 依赖因新建空数组而反复变化。 */
const EMPTY_REPORTS: ResearchReportSummary[] = []

/** 研报类型 → 中文徽标文案（枚举值只保留中文释义）。 */
const REPORT_TYPE_LABELS: Record<string, string> = {
  manual: '手动',
  asia_open: '亚盘',
  europe_open: '欧盘',
  us_open: '美盘',
}

/** 因果链验证状态 → 文案与配色（未知值原样灰显） */
const LINK_STATUS: Record<string, { label: string; className: string }> = {
  pending: { label: '待验证', className: 'border-zinc-600/50 bg-zinc-700/30 text-zinc-400' },
  verified: { label: '已确认', className: 'border-emerald-400/40 bg-emerald-400/10 text-emerald-300' },
  failed: { label: '已否决', className: 'border-rose-400/40 bg-rose-400/10 text-rose-300' },
  superseded: { label: '已被替代', className: 'border-zinc-700/40 bg-zinc-800/30 text-zinc-500' },
}

/** 因果链按主题分族：族内当前版在前、历史版（已被替代）在后，各自按 id 升序 */
function groupCausalLinks(links: CausalLinkView[]): [string, CausalLinkView[]][] {
  const groups = new Map<string, CausalLinkView[]>()
  for (const link of links) {
    const key = link.topic !== '' ? link.topic : '未分组'
    const arr = groups.get(key) ?? []
    arr.push(link)
    groups.set(key, arr)
  }
  return [...groups.entries()].map(([topic, arr]) => [
    topic,
    [...arr].sort((a, b) => {
      const aHist = a.status === 'superseded' ? 1 : 0
      const bHist = b.status === 'superseded' ? 1 : 0
      if (aHist !== bHist) return aHist - bHist
      return a.id - b.id
    }),
  ])
}

/** 族内反查"谁替代了 linkId"（supersedesId 匹配；无则 null） */
function findReplacer(links: CausalLinkView[], linkId: number): number | null {
  for (const link of links) if (link.supersedesId === linkId) return link.id
  return null
}

const BADGE_BASE = 'rounded border px-2 py-0.5 text-[10px] font-medium'

/** 判断报告类型是否为自定义调度生成的 UUID（兼容早期 custom_ 前缀）。 */
function isCustomScheduleType(type: string): boolean {
  return type.startsWith('custom_') || /^[0-9a-f]{8}-[0-9a-f-]{27}$/i.test(type)
}

/** 类型徽标：预设显示盘别、自定义项统一显示“自定义”，空串不渲染。 */
function TypeBadge({ type }: { type: string }) {
  if (type === '') return null
  const label = REPORT_TYPE_LABELS[type] ?? (isCustomScheduleType(type) ? '自定义' : type)
  return (
    <span className={`${BADGE_BASE} border-zinc-600/50 bg-zinc-700/30 text-zinc-400`}>
      {label}
    </span>
  )
}

/** 单个因果链节点 chip：kind 徽标 + 节点内容 + timeline 溯源小字（有值时） */
function ChainNodeChip({ node }: { node: ChainNode }) {
  return (
    <span className="inline-flex items-center gap-1 rounded border border-zinc-700/60 bg-zinc-800/40 px-2 py-1 text-[11px] leading-4 text-zinc-300">
      {node.kind !== '' && (
        <span className="rounded bg-zinc-700/60 px-1 text-[9px] text-zinc-400">{node.kind}</span>
      )}
      {node.node}
      {node.timeline_id !== undefined && (
        <span className="text-[9px] text-zinc-600">溯源 #{node.timeline_id}</span>
      )}
    </span>
  )
}

/** 一条因果链：状态/置信度/断点小字 + chip 节点链（→ 串联）+ 支撑证据（有值时） */
function CausalLinkCard({
  link,
  replacedById,
}: {
  link: CausalLinkView
  replacedById?: number | null
}) {
  const status = LINK_STATUS[link.status] ?? {
    label: link.status,
    className: 'border-zinc-600/50 bg-zinc-700/30 text-zinc-400',
  }
  return (
    <li className="rounded-lg border border-zinc-800 bg-zinc-950/60 px-3 py-2">
      <div className="flex flex-wrap items-center gap-2">
        <span className={`${BADGE_BASE} ${status.className}`}>{status.label}</span>
        {!link.awaitVerification && link.status !== 'superseded' && (
          <span className={`${BADGE_BASE} border-violet-500/30 bg-violet-500/10 text-violet-300`}>
            结论
          </span>
        )}
        <span className="text-[10px] tabular-nums text-zinc-500">链置信度 {Math.round(link.confidence * 100)}%</span>
        {link.brokenAt !== null && (
          <span className="text-[10px] text-rose-400/80">断点：第 {link.brokenAt + 1} 个节点</span>
        )}
        {link.supersedesId !== null && (
          <span className="text-[10px] text-amber-400/70">替代链#{link.supersedesId}</span>
        )}
        {replacedById != null && (
          <span className="text-[10px] text-zinc-500">已被链#{replacedById}替代</span>
        )}
      </div>
      <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
        {link.chain.map((node, i) => (
          <span key={i} className="inline-flex items-center gap-1.5">
            {i > 0 && <span className="text-[10px] text-zinc-600">→</span>}
            <ChainNodeChip node={node} />
          </span>
        ))}
      </div>
      {link.evidence.length > 0 && (
        <ul className="mt-1.5 space-y-0.5">
          {link.evidence.map((item, i) => (
            <li key={i} className="text-[11px] leading-5 text-zinc-500">· {item}</li>
          ))}
        </ul>
      )}
    </li>
  )
}

/** 单条研报：摘要行 + 展开详情（lazy 拉取全文，卡片生命周期内缓存） */
function ReportItem({
  report,
  expanded,
  onToggle,
}: {
  report: ResearchReportSummary
  expanded: boolean
  onToggle: () => void
}) {
  const [detail, setDetail] = useState<ResearchReportDetail | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // 展开时 lazy 拉取全文；loading 不进 deps（同 ReviewPanel：避免清理函数自我取消），fetchedRef 防重入
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
      .getResearchReport(report.id)
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

  const failed = report.error !== ''

  return (
    <article className="rounded-xl border border-zinc-800 bg-zinc-900/60 p-4">
      <button
        type="button"
        onClick={failed ? undefined : onToggle}
        disabled={failed}
        aria-expanded={failed ? false : expanded}
        className="block w-full text-left disabled:cursor-default"
      >
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-mono text-[11px] tabular-nums text-zinc-500">{fmtTime(report.time)}</span>
          <TypeBadge type={report.reportType} />
          {!failed && <ResearchAssetBadges assets={report.assetViews} />}
          {!failed && (
            <span className={'ml-auto text-[10px] text-zinc-600 transition-transform ' + (expanded ? 'rotate-90' : '')}>
              ▸
            </span>
          )}
        </div>
        {failed ? (
          <p className="mt-2 text-xs text-rose-400">研报失败：{report.error}</p>
        ) : (
          report.summary !== '' && (
            <p className="mt-2 line-clamp-2 text-[13px] leading-6 text-zinc-400">{report.summary}</p>
          )
        )}
      </button>

      {!failed && expanded && (
        <div className="mt-3 border-t border-zinc-800/80 pt-3">
          {loading && <p className="py-3 text-xs text-zinc-500">研报全文加载中…</p>}
          {error && <p className="py-3 text-xs text-rose-400">加载失败：{error}</p>}
          {detail && (
            <>
              <ResearchAssetDetails
                summary={detail.summary}
                crossMarketView={detail.crossMarketView}
                globalRisks={detail.globalRisks}
                assets={detail.assetViews}
              />
              {detail.causalLinks.length > 0 && (
                <div className="mt-3">
                  <h4 className="text-[10px] text-zinc-500">因果链（按主题分族）</h4>
                  <div className="mt-1.5 space-y-3">
                    {groupCausalLinks(detail.causalLinks).map(([topic, links]) => (
                      <div key={topic}>
                        <p className="mb-1 text-[10px] text-zinc-500">{topic}</p>
                        <ul className="space-y-2">
                          {links.map((link) => (
                            <CausalLinkCard
                              key={link.id}
                              link={link}
                              replacedById={findReplacer(links, link.id)}
                            />
                          ))}
                        </ul>
                      </div>
                    ))}
                  </div>
                </div>
              )}
              {detail.roundId !== '' ? (
                <ReviewToolChain roundId={detail.roundId} />
              ) : (
                <p className="mt-3 text-xs text-zinc-600">该研报无工具调用记录</p>
              )}
            </>
          )}
        </div>
      )}
    </article>
  )
}

/** 研报面板：列表分页自管，「生成研报」成功后回到第一页看最新研报。 */
export default function ResearchPanel() {
  const [total, setTotal] = useState(0)
  const [expandedId, setExpandedId] = useState<number | null>(null)
  const [running, setRunning] = useState(false)
  const [notice, setNotice] = useState<{ ok: boolean; text: string } | null>(null)
  const { page, totalPages, goToPage } = usePageState(total, PAGE_SIZE)
  const query = useApiData(() => api.getResearchReports(page * PAGE_SIZE, PAGE_SIZE), [page])
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

  /** 新研报出现在最前：收起展开项，回第一页（deps 驱动刷新）；已在第一页则原地刷新。 */
  const refreshToLatest = useCallback(() => {
    setExpandedId(null)
    if (page !== 0 && totalPages > 0) goToPage(0)
    else reload()
  }, [goToPage, page, reload, totalPages])

  /** 手动触发研报（manual + 最近 24 小时）：点火即返回，按钮立即恢复；进度经下方状态条呈现，成败报告落库后经 onFinished 刷新列表；409/503/422 经 ApiError.detail 提示。 */
  const runNow = async () => {
    setRunning(true)
    setNotice(null)
    try {
      await api.runResearch('manual', 24)
      setNotice({ ok: true, text: '研报已启动，进度见下方状态条' })
    } catch (e) {
      setNotice({ ok: false, text: e instanceof Error ? e.message : String(e) })
    } finally {
      setRunning(false)
    }
  }

  return (
    <section className="rounded-xl border border-zinc-800 bg-zinc-950/80 p-5 shadow-lg shadow-black/30">
      <header className="mb-4 flex flex-wrap items-center gap-2">
        <h2 className="text-sm font-semibold text-zinc-200">
          研报面板 <span className="text-xs font-normal text-zinc-500">— 宏观与消息面前瞻</span>
        </h2>
        <span className="ml-auto text-xs tabular-nums text-zinc-500">共 {total} 条研报</span>
        <button
          type="button"
          disabled={running}
          onClick={runNow}
          className="rounded-lg border border-violet-400/50 bg-violet-400/10 px-3 py-1.5 text-xs text-violet-300 transition hover:bg-violet-400/20 disabled:opacity-40"
        >
          {running ? '生成中…' : '生成研报'}
        </button>
      </header>

      <ResearchLiveStrip onFinished={refreshToLatest} />

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
          itemLabel="研报"
          loading={query.loading}
          onPageChange={changePage}
        />
      </StateHint>
    </section>
  )
}
