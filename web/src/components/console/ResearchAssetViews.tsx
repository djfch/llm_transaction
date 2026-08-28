/** 研报逐标的结论展示：徽标（列表摘要）+ 详情卡（结构/叙事/证据/风险 + 研报复盘记录）。
 *  复盘块仅在 asset.researchReviews 非空时渲染；客观结果(outcome)摘要与后端 _format_outcome 同口径。 */
import type { ResearchAssetDetail, ResearchAssetSummary, ResearchReviewItem } from '../../api/types'
import { fmtTime } from '../../utils/format'

const directionStyle: Record<string, string> = {
  偏多: 'border-emerald-400/40 bg-emerald-400/10 text-emerald-300',
  偏空: 'border-rose-400/40 bg-rose-400/10 text-rose-300',
  中性: 'border-zinc-600/50 bg-zinc-700/30 text-zinc-400',
}

export function ResearchAssetBadges({ assets }: { assets: ResearchAssetSummary[] }) {
  return (
    <>
      {assets.map((asset) => (
        <span
          key={asset.contract}
          className={'rounded border px-2 py-0.5 text-[10px] font-medium ' +
            (directionStyle[asset.direction] ?? directionStyle.中性)}
        >
          {asset.contract} · {asset.direction}
        </span>
      ))}
    </>
  )
}

function ListBlock({ title, items }: { title: string; items: string[] }) {
  if (items.length === 0) return null
  return (
    <div className="mt-2">
      <p className="text-[10px] text-zinc-500">{title}</p>
      <ul className="mt-1 space-y-0.5">
        {items.map((item, index) => (
          <li key={index} className="text-xs leading-5 text-zinc-400">· {item}</li>
        ))}
      </ul>
    </div>
  )
}

/** 客观结果数据状态：后端枚举 complete/partial/unavailable/pending 只保留中文（未知值原样显示） */
const DATA_STATUS_TEXT: Record<string, string> = {
  complete: '完整',
  partial: '部分',
  unavailable: '不可用',
  pending: '未到期',
}

/** 客观行情结果一行摘要：与后端 _format_outcome 同口径（无价格数据时只呈现状态与说明） */
function outcomeSummary(outcome: Record<string, unknown>): string {
  const rawStatus = String(outcome.data_status ?? 'unknown')
  const status = DATA_STATUS_TEXT[rawStatus] ?? rawStatus
  if (outcome.start_price == null) {
    const error = String(outcome.error ?? '')
    return `数据状态 ${status}（${error !== '' ? error : '无价格数据'}）`
  }
  const head =
    `数据状态 ${status}（K线 ${String(outcome.candles_actual)}/${String(outcome.candles_expected)}）` +
    ` | 起价 ${String(outcome.start_price)}`
  const tail =
    ` | 区间最高 ${String(outcome.high)}（${String(outcome.max_up_pct)}%）` +
    ` | 区间最低 ${String(outcome.low)}（${String(outcome.max_down_pct)}%）`
  if (outcome.end_price == null) {
    // 窗口末端无完整 K 线：止价与涨跌幅缺失，只呈现起价与区间高低
    const error = String(outcome.error ?? '') || '止价缺失'
    return `${head} → ${error}${tail}`
  }
  return `${head} → 止价 ${String(outcome.end_price)} | 涨跌 ${String(outcome.return_pct)}%${tail}`
}

/** 复盘枚举 → 中文释义（用户可见只显示中文；未知/空值原样透出便于排查） */
const REVIEW_ENUM_TEXT: Record<string, string> = {
  realized: '兑现',
  diverged: '背离',
  digested: '震荡消化',
  invalidated: '失效',
  sound: '成立',
  partial: '部分成立',
  flawed: '有缺陷',
  unsupported: '不支撑',
  counterevidence: '构成反证',
  partially_supported: '部分支撑',
  supported: '支撑结论',
  appropriate: '匹配合理',
  too_high: '偏高',
  too_low: '偏低',
  confirmed: '已证实',
  contradicted: '已证伪',
  unverifiable: '无法核实',
  unreviewable: '无法评价',
}

/** 枚举值显示：有中文释义取释义，否则原样返回（空串由调用方跳过渲染） */
function enumText(value: string): string {
  return REVIEW_ENUM_TEXT[value] ?? value
}

/** 复盘字段行：「标签：内容」（内容为空时由调用方跳过，不渲染） */
function ReviewRow({ label, text }: { label: string; text: string }) {
  return (
    <div className="flex gap-1.5 text-[11px] leading-4">
      <span className="shrink-0 text-zinc-500">{label}：</span>
      <span className="text-zinc-300">{text}</span>
    </div>
  )
}

/** 单条研报复盘卡：复盘时间/来源报告 + 三维枚举评价与理由 + 逐条依据评价 + 客观结果摘要 */
function ReviewCard({ review }: { review: ResearchReviewItem }) {
  return (
    <div className="mt-2 rounded-lg border border-zinc-800 bg-zinc-900/40 p-2.5">
      <p className="text-[10px] text-zinc-500">
        复盘 · {fmtTime(review.createdAt)} · 复盘报告 #{review.reviewReportId}
      </p>
      <div className="mt-1 space-y-1">
        {review.directionRelation !== '' && (
          <ReviewRow label="方向关系" text={`${enumText(review.directionRelation)}（${review.directionReason}）`} />
        )}
        {review.reasoningQuality !== '' && (
          <ReviewRow label="推理质量" text={`${enumText(review.reasoningQuality)}（${review.reasoningReview}）`} />
        )}
        {review.confidenceAssessment !== '' && (
          <ReviewRow label="置信度合规" text={`${enumText(review.confidenceAssessment)}（${review.confidenceReason}）`} />
        )}
        {review.improvementAdvice !== '' && <ReviewRow label="改进建议" text={review.improvementAdvice} />}
      </div>
      {review.evidenceReviews.length > 0 && (
        <div className="mt-1.5">
          <p className="text-[10px] text-zinc-500">依据逐条评价</p>
          <ul className="mt-0.5 space-y-0.5">
            {review.evidenceReviews.map((item) => (
              <li key={item.evidenceIndex} className="text-[11px] leading-4 text-zinc-400">
                #{item.evidenceIndex} 事实：{enumText(item.factStatus)} · 推理：{enumText(item.reasoningStatus)} —— {item.explanation}
              </li>
            ))}
          </ul>
        </div>
      )}
      {Object.keys(review.outcome).length > 0 && (
        <p className="mt-1.5 text-[11px] leading-4 text-zinc-400">客观结果：{outcomeSummary(review.outcome)}</p>
      )}
    </div>
  )
}

function AssetCard({ asset }: { asset: ResearchAssetDetail }) {
  return (
    <section className="rounded-lg border border-zinc-800 bg-zinc-950/50 p-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-xs text-zinc-200">{asset.contract}</span>
        <span className={'rounded border px-2 py-0.5 text-[10px] ' +
          (directionStyle[asset.direction] ?? directionStyle.中性)}
        >
          {asset.direction} · {asset.confidence}
        </span>
        <span className="text-[10px] text-zinc-500">{asset.horizon}</span>
      </div>
      <p className="mt-2 text-[11px] leading-5 text-zinc-500">
        结构：{asset.marketRegime || '未判断'} · 依据：{asset.basisType || '未说明'} ·
        技术确认：{asset.technicalConfirmation || '未说明'} · 数据：{asset.dataStatus || '未知'}
      </p>
      {(asset.narrative ?? '') !== '' && (
        <p className="mt-2 whitespace-pre-wrap text-xs leading-5 text-zinc-300">{asset.narrative}</p>
      )}
      <ListBlock title="证据" items={asset.evidence ?? []} />
      <ListBlock title="风险" items={asset.risks ?? []} />
      {/* 研报复盘记录（无复盘为 []/缺省，不渲染） */}
      {(asset.researchReviews ?? []).map((review) => (
        <ReviewCard key={review.id} review={review} />
      ))}
    </section>
  )
}

export function ResearchAssetDetails({
  summary,
  crossMarketView,
  globalRisks,
  assets,
}: {
  summary: string
  crossMarketView: string
  globalRisks: string[]
  assets: ResearchAssetDetail[]
}) {
  return (
    <div className="space-y-3">
      {summary !== '' && <p className="text-sm leading-6 text-zinc-300">{summary}</p>}
      {crossMarketView !== '' && <p className="text-xs text-zinc-400">跨标的观察：{crossMarketView}</p>}
      <ListBlock title="全局风险" items={globalRisks} />
      {assets.map((asset) => <AssetCard key={asset.contract} asset={asset} />)}
    </div>
  )
}
