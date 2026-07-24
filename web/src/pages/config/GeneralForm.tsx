/**
 * 常规设置表单：运行模式、LLM provider/model、通知开关。
 * 方案 C 抽屉样式：微型标签 + 紫色主按钮，标签直接用变量名。
 */
import { useState } from 'react'
import type { AppConfig } from '../../api/types'

const inputCls =
  'w-full rounded-lg border border-white/10 bg-zinc-900 px-3 py-2 font-mono text-sm text-zinc-100 focus:border-violet-400/60 focus:outline-none'
const labelCls = 'mb-1 block text-[10px] text-zinc-500'

export default function GeneralForm({
  initial,
  onSave,
}: {
  initial: AppConfig
  onSave: (config: AppConfig) => Promise<void>
}) {
  const [form, setForm] = useState<AppConfig>(() => structuredClone(initial))
  const [pending, setPending] = useState(false)
  const [savedAt, setSavedAt] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const patchLlm = (patch: Partial<AppConfig['llm']>) => {
    setForm((f) => ({ ...f, llm: { ...f.llm, ...patch } }))
    setSavedAt(null)
  }

  const handleSave = async () => {
    setPending(true)
    setError(null)
    try {
      await onSave(form)
      setSavedAt(new Date().toLocaleTimeString('zh-CN', { hour12: false }))
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setPending(false)
    }
  }

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <label className="block">
          <span className={labelCls}>mode</span>
          <select
            value={form.mode}
            onChange={(e) => {
              setForm((f) => ({ ...f, mode: e.target.value }))
              setSavedAt(null)
            }}
            className={inputCls}
          >
            <option value="paper">paper</option>
            <option value="testnet">testnet</option>
            <option value="live" disabled>
              live
            </option>
          </select>
          <span className="mt-1 block text-[10px] text-zinc-600">
            live 实盘需手动改 config.yaml 并重启
          </span>
        </label>

        <label className="block">
          <span className={labelCls}>llm.provider</span>
          <select
            value={form.llm.provider}
            onChange={(e) => patchLlm({ provider: e.target.value })}
            className={inputCls}
          >
            <option value="anthropic">anthropic</option>
            <option value="openai_compat">openai_compat</option>
          </select>
        </label>

        <label className="block">
          <span className={labelCls}>llm.model</span>
          <input
            value={form.llm.model}
            onChange={(e) => patchLlm({ model: e.target.value })}
            className={inputCls}
          />
        </label>

        <label className="block">
          <span className={labelCls}>llm.max_tokens</span>
          <input
            value={String(form.llm.max_tokens)}
            inputMode="numeric"
            onChange={(e) => patchLlm({ max_tokens: Number(e.target.value) || 0 })}
            className={inputCls}
          />
        </label>

        <label className="block">
          <span className={labelCls}>llm.openai_base_url</span>
          <input
            value={form.llm.openai_base_url}
            onChange={(e) => patchLlm({ openai_base_url: e.target.value })}
            placeholder="https://…（可空）"
            className={inputCls}
          />
        </label>

        <label className="block">
          <span className={labelCls}>llm.max_consecutive_failures</span>
          <input
            value={String(form.llm.max_consecutive_failures)}
            inputMode="numeric"
            onChange={(e) => patchLlm({ max_consecutive_failures: Number(e.target.value) || 0 })}
            className={inputCls}
          />
        </label>
      </div>

      <label className="flex items-center gap-2 text-xs text-zinc-300">
        <input
          type="checkbox"
          checked={form.notify.telegram_enabled}
          onChange={(e) => {
            setForm((f) => ({ ...f, notify: { ...f.notify, telegram_enabled: e.target.checked } }))
            setSavedAt(null)
          }}
          className="h-4 w-4 accent-violet-500"
        />
        notify.telegram_enabled
      </label>

      <div className="flex items-center gap-3">
        <button
          type="button"
          disabled={pending}
          onClick={handleSave}
          className="rounded-lg border border-violet-400/50 bg-violet-400/10 px-3 py-1.5 text-xs text-violet-300 transition hover:bg-violet-400/20 disabled:opacity-40"
        >
          {pending ? '保存中…' : '保存常规设置'}
        </button>
        {savedAt && <span className="text-xs text-emerald-400">已保存 {savedAt}</span>}
        {error && <span className="text-xs text-rose-400">保存失败：{error}</span>}
      </div>
    </div>
  )
}
