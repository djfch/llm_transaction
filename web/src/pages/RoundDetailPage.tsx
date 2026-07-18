/**
 * 决策轮审计详情：完整 prompt、LLM 原始输出、工具调用链逐级展开。
 */
import { Link, useParams } from 'react-router-dom'
import { api } from '../api'
import { useApiData } from '../hooks/useApiData'
import Card from '../components/Card'
import StateHint from '../components/StateHint'
import ToolCallItem from '../components/ToolCallItem'

export default function RoundDetailPage() {
  const { roundId = '' } = useParams()
  const query = useApiData(() => api.getRound(roundId), [roundId])
  const detail = query.data

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-3">
        <Link to="/rounds" className="text-sm text-sky-400 hover:underline">
          ← 返回决策时间线
        </Link>
        <h2 className="font-mono text-sm text-slate-400">{roundId}</h2>
      </div>

      <StateHint loading={query.loading} error={query.error}>
        {detail && (
          <>
            <Card title="prompt_snapshot(完整 Prompt 快照)">
              <details>
                <summary className="cursor-pointer text-xs text-slate-500">
                  点击展开/收起（{detail.prompt_snapshot.length} 字符）
                </summary>
                <pre className="mt-3 max-h-96 overflow-auto whitespace-pre-wrap rounded bg-slate-950 p-4 text-xs leading-relaxed text-slate-300">
                  {detail.prompt_snapshot}
                </pre>
              </details>
            </Card>

            <Card title="llm_raw(LLM 原始输出)">
              <pre className="max-h-96 overflow-auto whitespace-pre-wrap rounded bg-slate-950 p-4 text-xs leading-relaxed text-slate-300">
                {detail.llm_raw}
              </pre>
            </Card>

            <Card title={`tool_calls(工具调用链) · ${detail.tool_calls.length} 次`}>
              <div className="space-y-3">
                {detail.tool_calls.map((c) => (
                  <ToolCallItem key={c.seq} call={c} />
                ))}
              </div>
            </Card>
          </>
        )}
      </StateHint>
    </div>
  )
}
