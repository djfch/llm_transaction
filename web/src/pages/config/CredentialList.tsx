/**
 * LLM 凭证列表：每条凭证一张卡片——名称 / provider / model / api_key_env / key 状态 /
 * 被引用 agent 徽标；「编辑」按钮在卡片下方内联展开 CredentialForm（edit 模式，初值从
 * config.llm.credentials 按 name 查找，查不到即 default 合成凭证时回退 config.llm 平铺字段）；
 * 删除走 DELETE /api/credentials（服务端保存定义列表后尝试热重建），仅未被任何 agent 引用
 * （used_by 为空）且配置已加载时可用；删除已生效但热重建失败（llm_error）的警告提升到
 * 列表级展示——行内 state 会随刷新后卡片卸载而丢失，因此错误状态提升到列表层。
 * 本文件同时导出 StateText / SaveFeedback 供 SecretsForm（旧表单路径）复用，保持单向依赖。
 */
import { useState } from 'react'
import { api } from '../../api'
import type { AppConfig, CredentialStatus, SetSecretsResult } from '../../api/types'
import CredentialForm, { type CredentialEditInitial } from './CredentialForm'

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

/**
 * 解析编辑初值：优先从 config.llm.credentials 按 name 取完整定义；
 * 查不到（credentials 为空、后端合成 default 凭证）时回退 config.llm 平铺字段。
 */
function resolveEditInitial(cred: CredentialStatus, config: AppConfig): CredentialEditInitial {
  const defined = config.llm.credentials?.find((c) => c.name === cred.name)
  if (defined) {
    return {
      name: defined.name,
      provider: defined.provider,
      model: defined.model,
      max_tokens: defined.max_tokens,
      openai_base_url: defined.openai_base_url,
      thinking_effort: defined.thinking_effort ?? '',
    }
  }
  return {
    name: cred.name,
    provider: config.llm.provider as CredentialEditInitial['provider'],
    model: config.llm.model,
    max_tokens: config.llm.max_tokens,
    openai_base_url: config.llm.openai_base_url,
    thinking_effort: config.llm.thinking_effort ?? '',
  }
}

/** 单条凭证卡片：内联展开编辑表单 + 删除（引用中禁用） */
function CredentialRow({
  cred,
  config,
  onSaved,
  onDeleteError,
}: {
  cred: CredentialStatus
  /** 编辑初值来源（credentials 段或平铺 llm 字段）；null 时编辑/删除不可用 */
  config: AppConfig | null
  onSaved: () => void
  /** 删除已生效但热重建失败时上报列表级展示（本卡片随后随刷新卸载，行内 state 留不住） */
  onDeleteError: (msg: string) => void
}) {
  const [editing, setEditing] = useState(false)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null) // 删除的请求级失败（卡片仍在，行内展示）

  const canDelete = cred.used_by.length === 0 && config !== null

  const handleDelete = async () => {
    if (!window.confirm(`确认删除凭证「${cred.name}」？`)) return
    setPending(true)
    setError(null)
    try {
      // 专用端点负责保存删除结果并尝试热重建，前端无需 read-modify-write
      const res = await api.deleteCredential(cred.name)
      if (res.llm_error) onDeleteError(res.llm_error)
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
        <button
          type="button"
          disabled={pending || config === null}
          title={config === null ? '配置尚未加载，暂不可编辑' : undefined}
          onClick={() => setEditing((v) => !v)}
          className={btnCls}
        >
          编辑
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
      {editing && config !== null && (
        <CredentialForm
          mode="edit"
          initial={resolveEditInitial(cred, config)}
          keyConfigured={cred.key_configured}
          onSaved={onSaved}
          onCancel={() => setEditing(false)}
        />
      )}
      {error && (
        <p
          role="alert"
          className="rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-xs text-rose-300"
        >
          {error}
        </p>
      )}
    </li>
  )
}

/** 凭证列表：卡片按名称作 key，编辑展开状态各卡片独立；删除热重建失败的警告在列表级常驻 */
export default function CredentialList({
  credentials,
  config,
  onSaved,
}: {
  credentials: CredentialStatus[]
  config: AppConfig | null
  onSaved: () => void
}) {
  // 删除已生效但热重建失败的警告：被删卡片随 onSaved 刷新卸载后，警告仍在此处可见
  const [deleteError, setDeleteError] = useState<string | null>(null)
  return (
    <div className="space-y-2 border-t border-white/5 pt-4">
      <h3 className="text-xs text-zinc-300">LLM 凭证</h3>
      {deleteError && (
        <p
          role="alert"
          className="rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-300"
        >
          凭证已删除，但 LLM 热重建失败：{deleteError}
        </p>
      )}
      <ul className="space-y-2">
        {credentials.map((cred) => (
          <CredentialRow
            key={cred.name}
            cred={cred}
            config={config}
            onSaved={onSaved}
            onDeleteError={setDeleteError}
          />
        ))}
      </ul>
    </div>
  )
}
