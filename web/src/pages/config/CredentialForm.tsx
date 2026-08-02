/**
 * LLM 凭证统一表单（create / edit 双模式）：
 * - create：名称输入（小写字母数字连字符，实时校验 + 重名预校验 + api_key_env 推导预览）；
 * - edit：名称锁定为只读文本（name 是 agents 引用外键与 api_key_env 推导来源，改名走删除重建）。
 * 共用字段：provider 下拉 / model / max_tokens / openai_base_url（provider 非 anthropic 时显示，
 * openai_compat 必填）/ api_key（password，留空 = 不写 .env）。
 * 提交走专用端点（服务端顺序保存定义与 key，再尝试热重建；失败会报告已完成部分）：
 * create → POST /api/credentials；edit → PUT /api/credentials/{name}（请求体无 name）。
 * 反馈条组件内自渲染：请求异常玫瑰条 / llm_error 非空琥珀条 / 成功 emerald 提示；
 * 成功后 create 清空表单、edit 经 onCancel 收起，并回调 onSaved 让宿主刷新状态。
 */
import { useState } from 'react'
import { api } from '../../api'
import type { CredentialConfig } from '../../api/types'

const inputCls =
  'w-full rounded-lg border border-white/10 bg-zinc-900 px-3 py-2 font-mono text-sm text-zinc-100 focus:border-violet-400/60 focus:outline-none'
const labelCls = 'mb-1 block text-[10px] text-zinc-500'
const btnCls =
  'rounded-lg border border-violet-400/50 bg-violet-400/10 px-3 py-1.5 text-xs text-violet-300 transition hover:bg-violet-400/20 disabled:opacity-40'

type Provider = CredentialConfig['provider']

/** 凭证名规则：小写字母 / 数字 / 连字符（与后端校验一致） */
const NAME_PATTERN = /^[a-z0-9-]+$/

/** 由凭证名推导 .env 变量名：大写 + 连字符转下划线（与后端缺省推导一致） */
function deriveEnv(name: string): string {
  return `LLM_KEY_${name.toUpperCase().replace(/-/g, '_')}`
}

/** create 模式 props：重名预校验名单 + 保存回调 */
interface CreateProps {
  mode: 'create'
  existingNames: string[] // 已存在凭证名（用于重名校验）
  onSaved: () => void
}

/** edit 模式初值：凭证完整定义（由调用方从 config 解析；name 仅作展示与提交寻址，不可改） */
export interface CredentialEditInitial {
  name: string
  provider: Provider
  model: string
  max_tokens: number
  openai_base_url: string
}

/** edit 模式 props：编辑初值 + key 配置状态 + 收起回调 */
interface EditProps {
  mode: 'edit'
  initial: CredentialEditInitial
  keyConfigured: boolean // 该凭证 key 是否已配置（决定 api_key 占位符）
  onSaved: () => void
  onCancel: () => void // 取消 / 保存成功后收起表单
}

export default function CredentialForm(props: CreateProps | EditProps) {
  const isCreate = props.mode === 'create'
  const [name, setName] = useState('')
  const [provider, setProvider] = useState<Provider>(isCreate ? 'anthropic' : props.initial.provider)
  const [model, setModel] = useState(isCreate ? '' : props.initial.model)
  const [maxTokens, setMaxTokens] = useState(isCreate ? '4096' : String(props.initial.max_tokens))
  const [baseUrl, setBaseUrl] = useState(isCreate ? '' : props.initial.openai_base_url)
  const [apiKey, setApiKey] = useState('')
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null) // 校验失败或请求级失败
  const [llmError, setLlmError] = useState<string | null>(null) // 已保存但 provider 热重建失败
  const [saved, setSaved] = useState(false)

  // 前端预校验（后端仍会再校验，双保险）；逐条给出首个错误
  const validate = (): string | null => {
    if (props.mode === 'create') {
      if (!NAME_PATTERN.test(name)) return '名称仅允许小写字母、数字与连字符'
      if (props.existingNames.includes(name)) return `凭证「${name}」已存在`
    }
    if (model.trim() === '') return 'model 不能为空'
    if (!Number.isInteger(Number(maxTokens)) || Number(maxTokens) <= 0) return 'max_tokens 需为正整数'
    if (provider === 'openai_compat' && baseUrl.trim() === '') return 'openai_compat 需填写 openai_base_url'
    return null
  }

  const handleSave = async () => {
    const invalid = validate()
    if (invalid) {
      setError(invalid)
      return
    }
    setPending(true)
    setError(null)
    setLlmError(null)
    setSaved(false)
    // 契约：api_key 去首尾空白后为空 = 不改动 .env，提交体剔除该键；非空提交 trim 后值；anthropic 一律不带 base_url
    const key = apiKey.trim()
    const body = {
      provider,
      model: model.trim(),
      max_tokens: Number(maxTokens),
      openai_base_url: provider !== 'anthropic' ? baseUrl.trim() : '',
      ...(key !== '' ? { api_key: key } : {}),
    }
    try {
      const res =
        props.mode === 'create'
          ? await api.createCredential({ name, ...body })
          : await api.updateCredential(props.initial.name, body)
      if (res.llm_error) {
        setLlmError(res.llm_error) // 定义已落盘但热重建失败：琥珀警告，不清空/不收起
      } else {
        setSaved(true)
        if (props.mode === 'create') {
          setName('')
          setModel('')
          setMaxTokens('4096')
          setBaseUrl('')
          setApiKey('')
        } else {
          props.onCancel() // 保存成功后收起编辑表单
        }
      }
      props.onSaved()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setPending(false)
    }
  }

  const keyPlaceholder = isCreate
    ? '可留空，稍后在编辑中补'
    : props.keyConfigured
      ? '已配置，留空保持不变'
      : '未配置，可在此设置'

  return (
    <div className={`space-y-3 ${isCreate ? 'border-t border-white/5 pt-4' : ''}`}>
      {isCreate && <h3 className="text-xs text-zinc-300">新增凭证</h3>}
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        {isCreate ? (
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
        ) : (
          <div>
            <span className={labelCls}>name</span>
            <p className="font-mono text-sm text-zinc-100">{props.initial.name}</p>
            <span className="mt-1 block text-[10px] text-zinc-600">名称不可修改，改名请删除后重建</span>
          </div>
        )}
        <label className="block">
          <span className={labelCls}>provider</span>
          <select
            value={provider}
            onChange={(e) => setProvider(e.target.value as Provider)}
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
        <label className="block sm:col-span-2">
          <span className={labelCls}>api_key</span>
          <input
            type="password"
            autoComplete="new-password"
            value={apiKey}
            placeholder={keyPlaceholder}
            onChange={(e) => setApiKey(e.target.value)}
            className={inputCls}
          />
        </label>
      </div>
      <div className="flex items-center gap-3">
        <button type="button" disabled={pending} onClick={handleSave} className={btnCls}>
          {pending ? '保存中…' : isCreate ? '保存新凭证' : '保存'}
        </button>
        {!isCreate && (
          <button
            type="button"
            disabled={pending}
            onClick={props.onCancel}
            className="rounded-lg border border-white/10 px-3 py-1.5 text-xs text-zinc-400 transition hover:bg-white/5 disabled:opacity-40"
          >
            取消
          </button>
        )}
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
      {llmError && (
        <p
          role="alert"
          className="rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-300"
        >
          凭证已保存，但 LLM 热重建失败：{llmError}
        </p>
      )}
      <p className="text-[10px] text-zinc-600">
        保存后凭证与 key 立即生效；定义写入 config.yaml，key 写入服务器 .env（不进 git，永不回显明文）。
      </p>
    </div>
  )
}
