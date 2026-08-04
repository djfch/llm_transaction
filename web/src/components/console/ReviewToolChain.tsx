/**
 * 复盘工具调用链：按 roundId 拉取该轮复盘的审计详情，内嵌在复盘报告展开区。
 * 「工具调用详情」折叠区默认收起（报告全文是主角，与时间线卡片默认打开相反，是有意的）；
 * 「完整对话」由 ConversationThread 自身折叠，默认收起。
 * 父组件（ReviewPanel）仅在报告展开且 roundId 非空时挂载本组件，挂载即拉一次，天然 lazy；
 * 收起即卸载，再展开会重新拉取（有意从简，与 ReportItem 的报告全文缓存口径不同）。
 */
import { useEffect, useRef, useState } from 'react'
import { api } from '../../api'
import type { RoundDetail } from '../../api/types'
import ConversationThread from './ConversationThread'
import { FoldSection } from './TimelineCard'
import ToolSteps from './ToolSteps'

export default function ReviewToolChain({ roundId }: { roundId: string }) {
  const [detail, setDetail] = useState<RoundDetail | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [toolsOpen, setToolsOpen] = useState(false) // 报告全文是主角，工具区默认收起（有意与时间线卡片相反）

  // 挂载即拉取一次：fetchedRef 防 StrictMode 双调用重入（同 TimelineCard）；
  // alive 标志清理：卸载后忽略迟到的响应（同 TimelineCard 的 lazy fetch 写法）
  const fetchedRef = useRef(false)
  useEffect(() => {
    if (fetchedRef.current) return
    fetchedRef.current = true
    let alive = true
    setLoading(true)
    setError(null)
    api
      .getRound(roundId)
      .then((d) => {
        if (alive) setDetail(d)
      })
      .catch((e: unknown) => {
        if (alive) setError(e instanceof Error ? e.message : String(e))
      })
      .finally(() => {
        if (alive) setLoading(false)
      })
    return () => {
      alive = false
    }
  }, [roundId])

  if (loading) return <p className="mt-3 py-3 text-xs text-zinc-500">工具调用链加载中…</p>
  if (error) return <p className="mt-3 py-3 text-xs text-rose-400">加载失败：{error}</p>
  if (!detail) return null

  return (
    <div className="mt-3">
      <FoldSection
        title={`工具调用详情 · tool_calls（${detail.tool_calls.length} 步）`}
        open={toolsOpen}
        onToggle={() => setToolsOpen((o) => !o)}
      >
        <ToolSteps toolCalls={detail.tool_calls} compact />
      </FoldSection>
      {/* 完整对话由 ConversationThread 自身折叠，默认收起 */}
      <div className="mt-2">
        <ConversationThread llmRaw={detail.llm_raw} toolCalls={detail.tool_calls} />
      </div>
    </div>
  )
}
