/**
 * 新增 LLM 凭证表单：名称（小写字母数字连字符）/ provider 下拉 / model / max_tokens /
 * openai_base_url（provider 非 anthropic 时显示）；api_key_env 由名称推导展示。
 * 保存经 PUT /api/config 提交追加后的 llm.credentials 全量列表（整体替换语义），
 * 成功后清空表单并回调 onSaved 让宿主刷新 config 与 secrets 状态。
 */
import { useState } from 'react'
import { api } from '../../api'
import type { AppConfig, CredentialConfig } from '../../api/types'

const inputCls =
  'w-full rounded-lg border border-white/10 bg-zinc-900 px-3 py-2 font-mono text-sm text-zinc-100 focus:border-violet-400/60 focus:outline-none'
const labelCls = 'mb-1 block text-[10px] text-zinc-500'

/** 凭证名规则：小写字母 / 数字 / 连字符（与后端校验一致） */
const NAME_PATTERN = /^[a-z0-9-]+$/

/** 由凭证名推导 .env 变量名：大写 + 连字符转下划线（与后端缺省推导一致） */
function deriveEnv(name: string): string {
  return `LLM_KEY_${name.toUpperCase().replace(/-/g, '_')}`
}

/**
 * 旧配置物化：与后端 src/config.py 的 resolve_credentials 逐字段对齐——
 * config 无 llm.credentials 时，旧平铺 llm 字段等价于一条 default 凭证。
 * 新增首条凭证必须把它一并提交，否则 agents 缺省引用 default 会被后端校验 422（回归 B1）。
 */
function materializeLegacyDefault(config: AppConfig): CredentialConfig {
  return {
    name: 'default',
    provider: config.llm.provider as CredentialConfig['provider'],
    model: config.llm.model,
    max_tokens: config.llm.max_tokens,
    openai_base_url: config.llm.openai_base_url,
    api_key_env: config.llm.provider === 'anthropic' ? 'ANTHROPIC_API_KEY' : 'OPENAI_API_KEY',
  }
}

/** 生效凭证基线：credentials 非空返回之；为空（旧配置）物化 default（与后端 resolve 同语义） */
function baseCredentials(config: AppConfig): CredentialConfig[] {
  return config.llm.credentials?.length ? config.llm.credentials : [materializeLegacyDefault(config)]
}

export default function NewCredentialForm({
  existingNames,
  config,
  onSaved,
}: {
  existingNames: string[] // 已存在凭证名（用于重名校验）
  /** PUT /api/config 提交载体；null 时表单不可用 */
  config: AppConfig | null
  onSaved: () => void
}) {
  const [name, setName] = useState('')
  const [provider, setProvider] = useState<CredentialConfig['provider']>('anthropic')
  const [model, setModel] = useState('')
  const [maxTokens, setMaxTokens] = useState('4096')
  const [baseUrl, setBaseUrl] = useState('')
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)

  // 前端预校验（后端仍会再校验，双保险）；逐条给出首个错误
  const validate = (): string | null => {
    if (!NAME_PATTERN.test(name)) return '名称仅允许小写字母、数字与连字符'
    if (existingNames.includes(name)) return `凭证「${name}」已存在`
    if (model.trim() === '') return 'model 不能为空'
    if (!Number.isInteger(Number(maxTokens)) || Number(maxTokens) <= 0) return 'max_tokens 需为正整数'
    if (provider === 'openai_compat' && baseUrl.trim() === '') return 'openai_compat 需填写 openai_base_url'
    return null
  }

  const handleSave = async () => {
    if (!config) return
    const invalid = validate()
    if (invalid) {
      setError(invalid)
      return
    }
    setPending(true)
    setError(null)
    setSaved(false)
    try {
      const next: CredentialConfig = {
        name,
        provider,
        model: model.trim(),
        max_tokens: Number(maxTokens),
        openai_base_url: provider !== 'anthropic' ? baseUrl.trim() : '',
        api_key_env: deriveEnv(name),
      }
      // 契约：llm.credentials 整体替换——基于服务器最新态做 read-modify-write，
      // 避免 config prop 旧快照在慢网/并发操作下静默丢凭证（回归 M4）
      const fresh = await api.getConfig()
      const credentials = [...baseCredentials(fresh), next]
      const res = await api.putConfig({ ...fresh, llm: { ...fresh.llm, credentials } })
      if (res.llm_error) {
        setError(res.llm_error)
      } else {
        setName('')
        setModel('')
        setMaxTokens('4096')
        setBaseUrl('')
        setSaved(true)
      }
      onSaved()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setPending(false)
    }
  }

  return (
    <div className="space-y-3 border-t border-white/5 pt-4">
      <h3 className="text-xs text-zinc-300">新增凭证</h3>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <div>
          <label className="block">
            <span className={labelCls}>name</span>
            <input
              value={name}
              placeholder="如 claude-main"
              onChange={(e) => {
                setName(e.target.value)
                setSaved(false)
              }}
              className={inputCls}
            />
          </label>
          {NAME_PATTERN.test(name) && (
            <span className="mt-1 block font-mono text-[10px] text-zinc-600">{deriveEnv(name)}</span>
          )}
        </div>
        <label className="block">
          <span className={labelCls}>provider</span>
          <select
            value={provider}
            onChange={(e) => setProvider(e.target.value as CredentialConfig['provider'])}
            className={inputCls}
          >
            <option value="anthropic">anthropic</option>
            <option value="openai_compat">openai_compat</option>
            <option value="openai_responses">openai_responses</option>
          </select>
        </label>
        <label className="block">
          <span className={labelCls}>model</span>
          <input value={model} onChange={(e) => setModel(e.target.value)} className={inputCls} />
        </label>
        <label className="block">
          <span className={labelCls}>max_tokens</span>
          <input
            value={maxTokens}
            inputMode="numeric"
            onChange={(e) => setMaxTokens(e.target.value)}
            className={inputCls}
          />
        </label>
        {provider !== 'anthropic' && (
          <label className="block sm:col-span-2">
            <span className={labelCls}>openai_base_url</span>
            <input
              value={baseUrl}
              placeholder="https://…"
              onChange={(e) => setBaseUrl(e.target.value)}
              className={inputCls}
            />
          </label>
        )}
      </div>
      <div className="flex items-center gap-3">
        <button
          type="button"
          disabled={pending || config === null}
          onClick={handleSave}
          className="rounded-lg border border-violet-400/50 bg-violet-400/10 px-3 py-1.5 text-xs text-violet-300 transition hover:bg-violet-400/20 disabled:opacity-40"
        >
          {pending ? '保存中…' : '保存新凭证'}
        </button>
        {saved && <span className="text-xs text-emerald-400">已保存</span>}
      </div>
      {error && (
        <p
          role="alert"
          className="rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-xs text-rose-300"
        >
          {error}
        </p>
      )}
      <p className="text-[10px] text-zinc-600">
        凭证保存后立即生效；key 需在上方对应凭证行内单独保存（写入服务器 .env，不进 git）。
      </p>
    </div>
  )
}
