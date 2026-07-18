/**
 * 决策轮审计详情：完整 prompt、LLM 原始输出、工具调用链逐级展开。
 */
import { Link, useParams } from 'react-router-dom'
import { api } from '../api'
import type { ToolCall } from '../api/types'
import { useApiData } from '../hooks/useApiData'
import Badge from '../components/Badge'
import Card from '../components/Card'
import StateHint from '../components/StateHint'

/** 单个工具调用卡片：<details> 折叠展示入参与结果 */
function ToolCallItem({ call }: { call: ToolCall }) {
  const denied = call.risk_verdict !== 'allow'
  return (
    <details className="group rounded-lg border border-slate-800 bg-slate-900/60" open={denied}>
      <summary className="flex cursor-pointer list-none items-center gap-3 px-4 py-3 text-sm">
        <span className="text-slate-500 transition-transform group-open:rotate-90">▸</span>
        <span className="font-mono text-xs text-slate-500">#{call.seq}</span>
        <span className="font-medium text-slate-200">{call.tool}</span>
        <Badge
          text={denied ? 'deny(风控拒绝)' : 'allow(风控放行)'}
          tone={denied ? 'danger' : 'ok'}
        />
        <span className="ml-auto text-xs tabular-nums text-slate-500">
          duration_ms(耗时)={call.duration_ms}ms
        </span>
      </summary>
      <div className="space-y-3 border-t border-slate-800 px-4 py-3 text-sm">
        <div>
          <div className="mb-1 text-xs text-slate-500">args(调用入参)</div>
          <pre className="overflow-x-auto rounded bg-slate-950 p-3 text-xs text-slate-300">
            {JSON.stringify(call.args, null, 2)}
          </pre>
        </div>
        {call.risk_reason && (
          <div>
            <div className="mb-1 text-xs text-slate-500">risk_reason(风控理由)</div>
            <p className="rounded bg-rose-500/10 p-3 text-xs text-rose-300">{call.risk_reason}</p>
          </div>
        )}
        <div>
          <div className="mb-1 text-xs text-slate-500">result(执行结果)</div>
          <p className="rounded bg-slate-950 p-3 text-xs text-slate-300">{call.result}</p>
        </div>
      </div>
    </details>
  )
}

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
