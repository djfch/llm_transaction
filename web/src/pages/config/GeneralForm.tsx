/**
 * 常规设置表单：运行模式、LLM provider/model、通知开关。
 */
import { useState } from 'react'
import type { AppConfig } from '../../api/types'

const inputCls =
  'w-full rounded-lg border border-slate-700 bg-slate-800 px-3 py-2 text-sm text-slate-100 focus:border-sky-500 focus:outline-none'

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
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
        <label className="block text-xs">
          <span className="mb-1 block text-slate-400">mode(运行模式)</span>
          <select
            value={form.mode}
            onChange={(e) => {
              setForm((f) => ({ ...f, mode: e.target.value }))
              setSavedAt(null)
            }}
            className={inputCls}
          >
            <option value="paper">paper(模拟盘)</option>
            <option value="testnet">testnet(沙盒)</option>
            <option value="live" disabled>
              live(实盘，需手动改 config.yaml)
            </option>
          </select>
        </label>

        <label className="block text-xs">
          <span className="mb-1 block text-slate-400">llm.provider(LLM 提供商)</span>
          <select
            value={form.llm.provider}
            onChange={(e) => patchLlm({ provider: e.target.value })}
            className={inputCls}
          >
            <option value="anthropic">anthropic</option>
            <option value="openai_compat">openai_compat(国产兼容接口)</option>
          </select>
        </label>

        <label className="block text-xs">
          <span className="mb-1 block text-slate-400">llm.model(模型名)</span>
          <input
            value={form.llm.model}
            onChange={(e) => patchLlm({ model: e.target.value })}
            className={inputCls}
          />
        </label>

        <label className="block text-xs">
          <span className="mb-1 block text-slate-400">llm.max_tokens(最大输出 token)</span>
          <input
            value={String(form.llm.max_tokens)}
            inputMode="numeric"
            onChange={(e) => patchLlm({ max_tokens: Number(e.target.value) || 0 })}
            className={inputCls}
          />
        </label>

        <label className="block text-xs">
          <span className="mb-1 block text-slate-400">
            llm.openai_base_url(兼容接口地址，可空)
          </span>
          <input
            value={form.llm.openai_base_url}
            onChange={(e) => patchLlm({ openai_base_url: e.target.value })}
            placeholder="https://…"
            className={inputCls}
          />
        </label>

        <label className="block text-xs">
          <span className="mb-1 block text-slate-400">
            llm.max_consecutive_failures(连续失败锁定阈值)
          </span>
          <input
            value={String(form.llm.max_consecutive_failures)}
            inputMode="numeric"
            onChange={(e) => patchLlm({ max_consecutive_failures: Number(e.target.value) || 0 })}
            className={inputCls}
          />
        </label>
      </div>

      <label className="flex items-center gap-2 text-sm text-slate-300">
        <input
          type="checkbox"
          checked={form.notify.telegram_enabled}
          onChange={(e) => {
            setForm((f) => ({ ...f, notify: { ...f.notify, telegram_enabled: e.target.checked } }))
            setSavedAt(null)
          }}
          className="h-4 w-4 accent-sky-500"
        />
        notify.telegram_enabled(Telegram 通知开关)
      </label>

      <div className="flex items-center gap-3">
        <button
          type="button"
          disabled={pending}
          onClick={handleSave}
          className="rounded-lg bg-sky-600 px-4 py-2 text-sm font-medium text-white hover:bg-sky-500 disabled:opacity-40"
        >
          {pending ? '保存中…' : '保存常规设置'}
        </button>
        {savedAt && <span className="text-xs text-emerald-400">已保存 {savedAt}</span>}
        {error && <span className="text-xs text-rose-400">保存失败：{error}</span>}
      </div>
    </div>
  )
}
