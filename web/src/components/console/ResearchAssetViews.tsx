import type { ResearchAssetDetail, ResearchAssetSummary } from '../../api/types'

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
