/**
 * 单个工具调用卡片：<details> 折叠展示入参与结果。
 * 供决策轮审计详情页与实时决策卡共用。
 */
import type { ToolCall } from '../api/types'
import Badge from './Badge'

/** 执行结果可能是字符串或已解析对象（契约允许两种形态），统一转为展示文本 */
function resultText(result: ToolCall['result']): string {
  return typeof result === 'string' ? result : JSON.stringify(result, null, 2)
}

export default function ToolCallItem({ call }: { call: ToolCall }) {
  // 风控判定三态：deny=拒绝 / allow=放行 / 空串=未入风控（非交易工具，或交易工具参数校验失败）
  const denied = call.risk_verdict === 'deny'
  const badge =
    call.risk_verdict === 'deny'
      ? { text: 'deny(风控拒绝)', tone: 'danger' as const }
      : call.risk_verdict === 'allow'
        ? { text: 'allow(风控放行)', tone: 'ok' as const }
        : { text: '免判(未入风控)', tone: 'neutral' as const }
  return (
    <details className="group rounded-lg border border-slate-800 bg-slate-900/60" open={denied}>
      <summary className="flex cursor-pointer list-none items-center gap-3 px-4 py-3 text-sm">
        <span className="text-slate-500 transition-transform group-open:rotate-90">▸</span>
        <span className="font-mono text-xs text-slate-500">#{call.seq}</span>
        <span className="font-medium text-slate-200">{call.tool}</span>
        <Badge text={badge.text} tone={badge.tone} />
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
          <p className="rounded bg-slate-950 p-3 text-xs text-slate-300">{resultText(call.result)}</p>
        </div>
      </div>
    </details>
  )
}
