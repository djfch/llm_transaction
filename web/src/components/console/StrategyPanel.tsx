/**
 * 策略面板（首屏左栏，只读）：默认展示当前生效的策略书全文（GET /api/strategy），
 * 下拉可切换查看任一历史版本全文（GET /api/strategy/versions/{id}，含 vN/来源/时间徽标）。
 * 刷新语义：refreshKey 变化（配置抽屉关闭 / WS 决策轮事件，由 ConsolePage 驱动）时
 * 重拉当前策略与版本表，并复位到「当前版本」视图（避免旧选择展示过期内容）。
 * 只读约束：编辑与回滚保留在配置中心（ConfigDrawer），本组件不调用任何写接口。
 */
import { useEffect, useState } from 'react'
import { api } from '../../api'
import { useApiData } from '../../hooks/useApiData'
import { fmtTime, strategyCreatorBadgeClass, strategyCreatorText } from '../../utils/format'
import StateHint from '../StateHint'

export default function StrategyPanel({
  refreshKey,
  onOpenConfig,
}: {
  refreshKey: number
  onOpenConfig: () => void
}) {
  const currentQ = useApiData(() => api.getStrategy(), [refreshKey])
  const versionsQ = useApiData(() => api.getStrategyVersions(), [refreshKey])
  const [selectedId, setSelectedId] = useState<number | null>(null) // null = 当前版本
  // 仅选中历史版本时才拉详情；useApiData 的 alive 清理保证快速连续切换时旧响应不覆盖新选择
  const detailQ = useApiData(
    () => (selectedId === null ? Promise.resolve(null) : api.getStrategyVersion(selectedId)),
    [selectedId],
  )

  // refreshKey 变化（抽屉内可能保存/回滚过策略）→ 复位到「当前版本」
  useEffect(() => {
    setSelectedId(null)
  }, [refreshKey])

  const versions = versionsQ.data ?? []

  return (
    <section className="rounded-xl border border-white/5 bg-zinc-900/60 p-4 backdrop-blur">
      <header className="mb-3 flex items-center gap-2">
        <h3 className="text-xs tracking-widest text-zinc-500">策略 · system_prompt</h3>
        <button
          type="button"
          onClick={onOpenConfig}
          className="ml-auto rounded border border-violet-400/50 bg-violet-400/10 px-2 py-0.5 text-[10px] text-violet-300 transition hover:bg-violet-400/20"
        >
          去配置中心修改
        </button>
      </header>

      {/* 版本表失败不阻断当前策略展示：非阻断提示，下拉整体降级隐藏 */}
      {versionsQ.error !== null && <p className="mb-2 text-[10px] text-rose-400">版本列表加载失败</p>}
      {versions.length > 0 && (
        <select
          aria-label="选择策略版本"
          value={selectedId === null ? '' : String(selectedId)}
          onChange={(e) => setSelectedId(e.target.value === '' ? null : Number(e.target.value))}
          className="mb-3 w-full rounded-lg border border-zinc-800 bg-zinc-950 px-2 py-1.5 text-[11px] text-zinc-300 focus:border-violet-400/60 focus:outline-none"
        >
          <option value="">当前版本</option>
          {versions.map((v) => (
            <option key={v.id} value={v.id}>
              v{v.id} · {strategyCreatorText(v.createdBy)} · {fmtTime(v.time)}
            </option>
          ))}
        </select>
      )}

      {selectedId !== null ? (
        <>
          {detailQ.loading && <p className="py-4 text-center text-xs text-zinc-500">版本内容加载中…</p>}
          {detailQ.error !== null && (
            <p className="py-4 text-center text-xs text-rose-400">加载失败：{detailQ.error}</p>
          )}
          {/* id 守卫：useApiData 重拉期间保留旧 data，只有数据与选中版本一致才渲染，
              避免加载窗口/失败终态下陈旧版本全文与下拉选中错配（同 StrategyVersions 拉 diff 前清空的防误读先例） */}
          {detailQ.data !== null && detailQ.data.id === selectedId && (
            <>
              <div className="mb-2 flex flex-wrap items-center gap-1.5">
                <span className="font-mono text-[11px] font-bold text-zinc-200">v{detailQ.data.id}</span>
                <span className={strategyCreatorBadgeClass(detailQ.data.createdBy)}>
                  {strategyCreatorText(detailQ.data.createdBy)}
                </span>
                <span className="rounded border border-amber-400/40 bg-amber-400/10 px-1.5 py-0.5 text-[10px] font-medium text-amber-300">
                  历史版本
                </span>
                <span className="ml-auto font-mono text-[10px] tabular-nums text-zinc-500">
                  {fmtTime(detailQ.data.time)}
                </span>
              </div>
              <pre className="max-h-80 overflow-auto whitespace-pre-wrap rounded-lg border border-zinc-800 bg-zinc-950 p-3 font-mono text-[11px] leading-5 text-zinc-300">
                {detailQ.data.content}
              </pre>
            </>
          )}
        </>
      ) : (
        // loading 门控 data === null：后台重拉（WS 决策轮/抽屉关闭）期间保留旧全文，
        // 不闪烁「加载中…」（同 ConfigDrawer 的 DrawerSection 保活先例）
        <StateHint loading={currentQ.loading && currentQ.data === null} error={currentQ.error} empty={currentQ.data === ''}>
          <pre className="max-h-80 overflow-auto whitespace-pre-wrap rounded-lg border border-zinc-800 bg-zinc-950 p-3 font-mono text-[11px] leading-5 text-zinc-300">
            {currentQ.data}
          </pre>
        </StateHint>
      )}
    </section>
  )
}
