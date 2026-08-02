/**
 * 密钥配置：交易所 / Telegram 为只读状态行（仅经 .env 配置）。
 * LLM 部分按配置形态分两支：
 * - 多凭证（secrets status 含 credentials）：凭证列表（内联编辑 / 删除）+ 新增凭证表单
 *   （统一走 POST/PUT/DELETE /api/credentials 专用端点，定义与 key 按顺序保存）；
 * - 旧版单凭证（无 credentials 或为空）：「default 凭证」引导提示 + 旧 ANTHROPIC/OPENAI 两输入框表单。
 * 响应永不包含密钥明文；error 非空（如 provider 重建失败）时展示错误条。
 * 使用技术配置键标签、等宽状态文字和紫色主按钮。
 */
import { useState } from 'react'
import { api } from '../../api'
import type { AppConfig, SecretsStatus, SetSecretsBody, SetSecretsResult } from '../../api/types'
import CredentialList, { SaveFeedback, StateText } from './CredentialList'
import CredentialForm from './CredentialForm'

const inputCls =
  'w-full rounded-lg border border-white/10 bg-zinc-900 px-3 py-2 font-mono text-sm text-zinc-100 focus:border-violet-400/60 focus:outline-none'
const labelCls = 'mb-1 block text-[10px] text-zinc-500'

// 只读状态行只展示交易所与 Telegram；LLM 由下方表单在线配置
const READONLY_ITEMS: Array<{ key: 'gate_key' | 'telegram'; label: string }> = [
  { key: 'gate_key', label: 'gate_key' },
  { key: 'telegram', label: 'telegram' },
]

/** 只读状态区：gate_key 与 telegram 仅显示是否配置，永不回显明文 */
function ReadonlyStatus({ status }: { status: SecretsStatus }) {
  return (
    <div>
      <ul className="space-y-2">
        {READONLY_ITEMS.map(({ key, label }) => (
          <li
            key={key}
            className="flex items-center rounded-lg border border-white/5 bg-zinc-900/60 px-3 py-2 text-sm text-zinc-300"
          >
            <span className="font-mono text-xs">{label}</span>
            <span className="ml-auto">
              <StateText configured={status[key]} />
            </span>
          </li>
        ))}
      </ul>
      <p className="mt-2 text-[10px] text-zinc-600">
        交易所与 Telegram 密钥仅经服务器 .env 配置，前端不提供修改入口，API 永不返回明文。
      </p>
    </div>
  )
}

/** LLM API Key 表单（旧版单凭证兼容路径）：ANTHROPIC_API_KEY / OPENAI_API_KEY，空串字段发送前剔除 */
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
    <div className="space-y-3 border-t border-white/5 pt-4">
      <div className="flex items-center gap-2">
        <h3 className="text-xs text-zinc-300">LLM API Key 在线配置</h3>
        <StateText configured={configured} />
      </div>
      <div className="grid grid-cols-1 gap-3">
        <label className="block">
          <span className={labelCls}>ANTHROPIC_API_KEY</span>
          <input
            type="password"
            autoComplete="new-password"
            value={anthropicKey}
            placeholder={placeholder}
            onChange={(e) => setAnthropicKey(e.target.value)}
            className={inputCls}
          />
        </label>
        <label className="block">
          <span className={labelCls}>OPENAI_API_KEY</span>
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
        className="rounded-lg border border-violet-400/50 bg-violet-400/10 px-3 py-1.5 text-xs text-violet-300 transition hover:bg-violet-400/20 disabled:opacity-40"
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
      <p className="text-[10px] text-zinc-600">
        LLM key 保存到服务器 .env（不进 git），重启后仍有效；留空的字段不改动。交易所 key 仍需手动编辑 .env。
      </p>
    </div>
  )
}

export default function SecretsForm({
  status,
  config,
  onSaved,
}: {
  status: SecretsStatus
  /** 凭证编辑初值来源（credentials 段或平铺 llm 字段）；config 尚未加载时为 null，对应按钮禁用 */
  config: AppConfig | null
  /** 保存成功后的回调（父级刷新密钥状态与配置） */
  onSaved: () => void
}) {
  // 防御：旧后端可能缺失 credentials 键，按旧版单凭证路径渲染
  const credentials = status.credentials ?? []

  return (
    <div className="space-y-5">
      <ReadonlyStatus status={status} />
      {credentials.length > 0 ? (
        <>
          <CredentialList credentials={credentials} config={config} onSaved={onSaved} />
          <CredentialForm mode="create" existingNames={credentials.map((c) => c.name)} onSaved={onSaved} />
        </>
      ) : (
        <>
          <p className="rounded-lg border border-white/5 bg-zinc-900/60 px-3 py-2 text-[10px] text-zinc-500">
            当前为旧版单凭证（default）配置：下方直接保存 Key 即可，决策与复盘 agent 共用该凭证；
            在 config.yaml 的 llm.credentials 登记多条凭证后，此处将切换为凭证管理界面。
          </p>
          <LlmKeyForm configured={status.llm_key} onSaved={onSaved} />
        </>
      )}
    </div>
  )
}
