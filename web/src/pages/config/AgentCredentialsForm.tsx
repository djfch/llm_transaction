/**
 * Agent 凭证分配：trader（决策）/ reviewer（复盘）/ researcher（研报）各一个下拉，
 * 选项来自凭证列表（旧配置无 credentials 时仅 default）；保存经 PUT /api/config 写 agents 段。
 * 表单为 props 驱动：选项与初值由宿主（ConfigDrawer）从 configQ/secretsQ 注入，
 * 保存结果（含 llm 热生效错误）由宿主统一展示，与常规设置同一模式。
 */
import { useState } from 'react'
import type { AppConfig } from '../../api/types'

const inputCls =
  'w-full rounded-lg border border-white/10 bg-zinc-900 px-3 py-2 font-mono text-sm text-zinc-100 focus:border-violet-400/60 focus:outline-none'
const labelCls = 'mb-1 block text-[10px] text-zinc-500'

/** agent 中文释义（枚举值按规范只保留中文；标签用配置键，释义放右侧提示） */
const AGENT_HINTS = {
  trader: '决策 agent',
  reviewer: '复盘 agent',
  researcher: '研报 agent',
} as const

type AgentId = keyof typeof AGENT_HINTS

export default function AgentCredentialsForm({
  initial,
  credentialNames,
  onSave,
}: {
  initial: AppConfig
  /** 可选凭证名列表（来自 secrets status；空 = 旧配置，仅 default 可选） */
  credentialNames: string[]
  onSave: (next: AppConfig) => Promise<void>
}) {
  const [trader, setTrader] = useState(initial.agents?.trader?.credential ?? 'default')
  const [reviewer, setReviewer] = useState(initial.agents?.reviewer?.credential ?? 'default')
  const [researcher, setResearcher] = useState(
    initial.agents?.researcher?.credential ?? 'default',
  )
  const [pending, setPending] = useState(false)
  const [savedAt, setSavedAt] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  // 每个下拉只并入自己的当前值：既能回显异常绑定，又不会把无效值传播给其他 Agent
  const optionsFor = (current: string) =>
    Array.from(
      new Set([...(credentialNames.length > 0 ? credentialNames : ['default']), current]),
    )

  const handleSave = async () => {
    setPending(true)
    setError(null)
    try {
      await onSave({
        ...initial,
        agents: {
          trader: { credential: trader },
          reviewer: { credential: reviewer },
          researcher: { credential: researcher },
        },
      })
      setSavedAt(new Date().toLocaleTimeString('zh-CN', { hour12: false }))
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setPending(false)
    }
  }

  const renderSelect = (
    id: AgentId,
    value: string,
    onChange: (v: string) => void,
  ) => (
    <div>
      <label className="block">
        <span className={labelCls}>{`agents.${id}.credential`}</span>
        <select
          value={value}
          onChange={(e) => {
            onChange(e.target.value)
            setSavedAt(null)
          }}
          className={inputCls}
        >
          {optionsFor(value).map((name) => (
            <option key={name} value={name}>
              {name}
            </option>
          ))}
        </select>
      </label>
      <span className="mt-1 block text-[10px] text-zinc-600">{AGENT_HINTS[id]}使用的 LLM 凭证</span>
    </div>
  )

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        {renderSelect('trader', trader, setTrader)}
        {renderSelect('reviewer', reviewer, setReviewer)}
        {renderSelect('researcher', researcher, setResearcher)}
      </div>
      <div className="flex items-center gap-3">
        <button
          type="button"
          disabled={pending}
          onClick={handleSave}
          className="rounded-lg border border-violet-400/50 bg-violet-400/10 px-3 py-1.5 text-xs text-violet-300 transition hover:bg-violet-400/20 disabled:opacity-40"
        >
          {pending ? '保存中…' : '保存凭证分配'}
        </button>
        {savedAt && <span className="text-xs text-emerald-400">已保存 {savedAt}</span>}
        {error && <span className="text-xs text-rose-400">保存失败：{error}</span>}
      </div>
    </div>
  )
}
