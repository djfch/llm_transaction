/**
 * 密钥配置：交易所 / Telegram 为只读状态徽标（仅经 .env 配置）；
 * LLM API Key 支持在线表单保存（POST /api/secrets，写入服务器 .env，不进 git，重启后仍有效）。
 * 响应永不包含密钥明文；error 非空（如 provider 重建失败）时展示错误条。
 */
import { useState } from 'react'
import { api } from '../../api'
import type { SecretsStatus, SetSecretsBody, SetSecretsResult } from '../../api/types'
import Badge from '../../components/Badge'

const inputCls =
  'w-full rounded-lg border border-slate-700 bg-slate-800 px-3 py-2 text-sm text-slate-100 focus:border-sky-500 focus:outline-none'

// 只读徽标：LLM 已改为下方表单在线配置，此处仅剩交易所与 Telegram
const READONLY_ITEMS: Array<{ key: 'gate_key' | 'telegram'; label: string }> = [
  { key: 'gate_key', label: 'gate_key(交易所 API Key)' },
  { key: 'telegram', label: 'telegram(Telegram Bot Token)' },
]

/** 只读状态徽标区：gate_key(交易所 API Key) 与 telegram(Telegram Bot Token) */
function ReadonlyBadges({ status }: { status: SecretsStatus }) {
  return (
    <div>
      <ul className="flex flex-wrap gap-3">
        {READONLY_ITEMS.map(({ key, label }) => (
          <li
            key={key}
            className="flex items-center gap-2 rounded-lg border border-slate-800 bg-slate-900/60 px-3 py-2 text-sm text-slate-300"
          >
            {label}
            <Badge text={status[key] ? '已配置' : '未配置'} tone={status[key] ? 'ok' : 'warn'} />
          </li>
        ))}
      </ul>
      <p className="mt-2 text-xs text-slate-500">
        交易所与 Telegram 密钥仅经服务器 .env 配置，前端不提供修改入口，API 永不返回明文。
      </p>
    </div>
  )
}

/** 保存结果反馈条：error 玫瑰色错误 > llm_configured=false 琥珀警告 > 成功提示 */
function SaveFeedback({ result }: { result: SetSecretsResult }) {
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

/** LLM API Key 表单：ANTHROPIC_API_KEY / OPENAI_API_KEY，空串字段发送前剔除 */
function LlmKeyForm({ configured, onSaved }: { configured: boolean; onSaved: () => void }) {
  const [anthropicKey, setAnthropicKey] = useState('')
  const [openaiKey, setOpenaiKey] = useState('')
  const [pending, setPending] = useState(false)
  const [result, setResult] = useState<SetSecretsResult | null>(null)
  const [requestError, setRequestError] = useState<string | null>(null) // 请求级失败（网络/非 2xx）

  // 占位符按当前配置状态提示（SecretsStatus 不区分具体 provider，两个输入框共用）
  const placeholder = configured ? '已配置，输入以更换' : '未配置'

  const handleSave = async () => {
    setPending(true)
    setResult(null)
    setRequestError(null)
    try {
      // 契约：空串 = 不改动该项，发送前剔除空串字段
      const body: SetSecretsBody = {}
      if (anthropicKey !== '') body.anthropic_api_key = anthropicKey
      if (openaiKey !== '') body.openai_api_key = openaiKey
      const res = await api.setSecrets(body)
      setResult(res)
      if (!res.error) {
        setAnthropicKey('')
        setOpenaiKey('')
        onSaved()
      }
    } catch (e) {
      setRequestError(e instanceof Error ? e.message : String(e))
    } finally {
      setPending(false)
    }
  }

  return (
    <div className="space-y-3 border-t border-slate-800 pt-4">
      <div className="flex items-center gap-2">
        <h3 className="text-sm font-medium text-slate-300">LLM API Key 在线配置</h3>
        <Badge text={configured ? '已配置' : '未配置'} tone={configured ? 'ok' : 'warn'} />
      </div>
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <label className="block text-xs">
          <span className="mb-1 block text-slate-400">ANTHROPIC_API_KEY(Anthropic 密钥)</span>
          <input
            type="password"
            autoComplete="new-password"
            value={anthropicKey}
            placeholder={placeholder}
            onChange={(e) => setAnthropicKey(e.target.value)}
            className={inputCls}
          />
        </label>
        <label className="block text-xs">
          <span className="mb-1 block text-slate-400">OPENAI_API_KEY(OpenAI 兼容接口密钥)</span>
          <input
            type="password"
            autoComplete="new-password"
            value={openaiKey}
            placeholder={placeholder}
            onChange={(e) => setOpenaiKey(e.target.value)}
            className={inputCls}
          />
        </label>
      </div>
      <button
        type="button"
        disabled={pending || (anthropicKey === '' && openaiKey === '')}
        onClick={handleSave}
        className="rounded-lg bg-sky-600 px-4 py-2 text-sm font-medium text-white hover:bg-sky-500 disabled:opacity-40"
      >
        {pending ? '保存中…' : '保存 LLM 密钥'}
      </button>
      {requestError && (
        <p
          role="alert"
          className="rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-xs text-rose-300"
        >
          保存失败：{requestError}
        </p>
      )}
      {result && <SaveFeedback result={result} />}
      <p className="text-xs text-slate-500">
        LLM key 保存到服务器 .env（不进 git），重启后仍有效；留空的字段不改动。交易所 key 仍需手动编辑 .env。
      </p>
    </div>
  )
}

export default function SecretsForm({
  status,
  onSaved,
}: {
  status: SecretsStatus
  /** 保存成功后的回调（父级刷新密钥状态） */
  onSaved: () => void
}) {
  return (
    <div className="space-y-5">
      <ReadonlyBadges status={status} />
      <LlmKeyForm configured={status.llm_key} onSaved={onSaved} />
    </div>
  )
}
