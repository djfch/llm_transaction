/**
 * LLM 凭证列表：每条凭证一张卡片——名称 / provider / model / api_key_env / key 状态 /
 * 被引用 agent 徽标；行内 password 输入 + 「保存 key」按钮（POST /api/secrets 的
 * credential/api_key 形式）；删除按钮仅未被任何 agent 引用（used_by 为空）且配置已加载时可用，
 * 删除经 PUT /api/config 提交移除该条后的 llm.credentials 全量列表。
 * 本文件同时导出 StateText / SaveFeedback 供 SecretsForm（旧表单路径）复用，保持单向依赖。
 */
import { useState } from 'react'
import { api } from '../../api'
import type { AppConfig, CredentialStatus, SetSecretsResult } from '../../api/types'

const inputCls =
  'w-full rounded-lg border border-white/10 bg-zinc-900 px-3 py-2 font-mono text-sm text-zinc-100 focus:border-violet-400/60 focus:outline-none'
const btnCls =
  'shrink-0 rounded-lg border border-violet-400/50 bg-violet-400/10 px-3 py-1.5 text-xs text-violet-300 transition hover:bg-violet-400/20 disabled:opacity-40'

/** 配置状态文字：已配置 emerald / 未配置 zinc，等宽 */
export function StateText({ configured }: { configured: boolean }) {
  return (
    <span className={`font-mono text-xs ${configured ? 'text-emerald-400' : 'text-zinc-500'}`}>
      {configured ? '已配置' : '未配置'}
    </span>
  )
}

/** 保存结果反馈条：error 玫瑰色错误 > llm_configured=false 琥珀警告 > 成功提示 */
export function SaveFeedback({ result }: { result: SetSecretsResult }) {
  if (result.error) {
    return (
      <p
        role="alert"
        className="rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-xs text-rose-300"
      >
        {result.error}
      </p>
    )
  }
  return (
    <>
      {!result.llm_configured && (
        <p
          role="alert"
          className="rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-300"
        >
          LLM 仍未配置：请确认提交的 Key 与当前 provider 匹配，自动决策将保持暂停。
        </p>
      )}
      <p className="text-xs text-emerald-400">已保存到服务器 .env</p>
    </>
  )
}

/** agent 引用徽标：trader→决策 / reviewer→复盘（枚举值按规范只保留中文释义） */
const AGENT_LABELS: Record<string, string> = { trader: '决策', reviewer: '复盘' }

function UsedByBadges({ usedBy }: { usedBy: string[] }) {
  if (usedBy.length === 0) return <span className="text-[10px] text-zinc-600">未被引用</span>
  return (
    <span className="flex gap-1">
      {usedBy.map((agent) => (
        <span
          key={agent}
          className="rounded border border-violet-400/40 bg-violet-400/10 px-1.5 py-0.5 text-[10px] text-violet-300"
        >
          {AGENT_LABELS[agent] ?? agent}
        </span>
      ))}
    </span>
  )
}

/** 单条凭证卡片：行内保存 key + 删除（引用中禁用） */
function CredentialRow({
  cred,
  config,
  onSaved,
}: {
  cred: CredentialStatus
  /** PUT /api/config 提交载体；null 时删除不可用 */
  config: AppConfig | null
  onSaved: () => void
}) {
  const [key, setKey] = useState('')
  const [pending, setPending] = useState(false)
  const [result, setResult] = useState<SetSecretsResult | null>(null)
  const [error, setError] = useState<string | null>(null) // 请求级失败或删除/热重建错误

  const canDelete = cred.used_by.length === 0 && config !== null

  const handleSaveKey = async () => {
    setPending(true)
    setResult(null)
    setError(null)
    try {
      const res = await api.setSecrets({ credential: cred.name, api_key: key })
      setResult(res)
      if (!res.error) {
        setKey('')
        onSaved()
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setPending(false)
    }
  }

  const handleDelete = async () => {
    if (!config || !window.confirm(`确认删除凭证「${cred.name}」？`)) return
    setPending(true)
    setError(null)
    try {
      // 契约：llm.credentials 整体替换——基于服务器最新态移除本条，
      // 避免 config prop 旧快照在连续删除/慢网下静默丢凭证（回归 M4）
      const fresh = await api.getConfig()
      const remaining = (fresh.llm.credentials ?? []).filter((c) => c.name !== cred.name)
      const res = await api.putConfig({ ...fresh, llm: { ...fresh.llm, credentials: remaining } })
      if (res.llm_error) setError(res.llm_error)
      onSaved()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setPending(false)
    }
  }

  return (
    <li className="space-y-2 rounded-lg border border-white/5 bg-zinc-900/60 px-3 py-2">
      <div className="flex items-center gap-2">
        <span className="font-mono text-xs text-zinc-100">{cred.name}</span>
        <UsedByBadges usedBy={cred.used_by} />
        <span className="ml-auto">
          <StateText configured={cred.key_configured} />
        </span>
      </div>
      <p className="font-mono text-[10px] text-zinc-500">
        {cred.provider} · {cred.model} · {cred.api_key_env}
      </p>
      <div className="flex items-center gap-2">
        <input
          type="password"
          autoComplete="new-password"
          aria-label={`${cred.name} 的 API Key`}
          value={key}
          placeholder={cred.key_configured ? '已配置，输入以更换' : '未配置'}
          onChange={(e) => setKey(e.target.value)}
          className={inputCls}
        />
        <button type="button" disabled={pending || key === ''} onClick={handleSaveKey} className={btnCls}>
          保存 key
        </button>
        <button
          type="button"
          disabled={pending || !canDelete}
          title={cred.used_by.length > 0 ? '仍被 agent 引用，请先在凭证分配中解除引用' : undefined}
          onClick={handleDelete}
          className="shrink-0 rounded-lg border border-rose-500/40 bg-rose-500/10 px-3 py-1.5 text-xs text-rose-300 transition hover:bg-rose-500/20 disabled:opacity-40"
        >
          删除
        </button>
      </div>
      {error && (
        <p
          role="alert"
          className="rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-xs text-rose-300"
        >
          {error}
        </p>
      )}
      {result && <SaveFeedback result={result} />}
    </li>
  )
}

/** 凭证列表：卡片按名称作 key，后台刷新保活行内输入状态 */
export default function CredentialList({
  credentials,
  config,
  onSaved,
}: {
  credentials: CredentialStatus[]
  config: AppConfig | null
  onSaved: () => void
}) {
  return (
    <div className="space-y-2 border-t border-white/5 pt-4">
      <h3 className="text-xs text-zinc-300">LLM 凭证</h3>
      <ul className="space-y-2">
        {credentials.map((cred) => (
          <CredentialRow key={cred.name} cred={cred} config={config} onSaved={onSaved} />
        ))}
      </ul>
    </div>
  )
}
