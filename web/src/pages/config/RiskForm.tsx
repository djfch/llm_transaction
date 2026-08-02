/**
 * 风控参数表单：字符串录入 + 即时校验，保存时合并回 RiskConfig（保留 kill_switch）。
 * 使用微型技术标签、等宽数字输入和紫色主按钮。
 */
import { useMemo, useState } from 'react'
import type { RiskConfig } from '../../api/types'
import { parseRisk, validateRisk, type RiskFormValues } from './validate'

/** 字段元数据：技术配置键直接作为标签，不追加括号注释。 */
const FIELDS: Array<{ key: keyof RiskFormValues; label: string }> = [
  { key: 'max_position_pct', label: 'max_position_pct' },
  { key: 'max_total_position_pct', label: 'max_total_position_pct' },
  { key: 'max_leverage', label: 'max_leverage' },
  { key: 'daily_loss_limit', label: 'daily_loss_limit' },
  { key: 'max_orders_per_day', label: 'max_orders_per_day' },
  { key: 'max_deviation', label: 'max_deviation' },
]

function toValues(risk: RiskConfig): RiskFormValues {
  return {
    max_position_pct: String(risk.max_position_pct),
    max_total_position_pct: String(risk.max_total_position_pct),
    max_leverage: String(risk.max_leverage),
    daily_loss_limit: String(risk.daily_loss_limit),
    max_orders_per_day: String(risk.max_orders_per_day),
    max_deviation: String(risk.max_deviation),
  }
}

export default function RiskForm({
  initial,
  onSave,
}: {
  initial: RiskConfig
  onSave: (risk: RiskConfig) => Promise<void>
}) {
  const [values, setValues] = useState<RiskFormValues>(() => toValues(initial))
  const [pending, setPending] = useState(false)
  const [savedAt, setSavedAt] = useState<string | null>(null)
  const [saveError, setSaveError] = useState<string | null>(null)

  const errors = useMemo(() => validateRisk(values), [values])
  const valid = Object.keys(errors).length === 0

  const set = (key: keyof RiskFormValues) => (e: React.ChangeEvent<HTMLInputElement>) => {
    setValues((v) => ({ ...v, [key]: e.target.value }))
    setSavedAt(null)
  }

  const handleSave = async () => {
    const parsed = parseRisk(values)
    if (!parsed) return
    setPending(true)
    setSaveError(null)
    try {
      await onSave({ ...initial, ...parsed })
      setSavedAt(new Date().toLocaleTimeString('zh-CN', { hour12: false }))
    } catch (e) {
      setSaveError(e instanceof Error ? e.message : String(e))
    } finally {
      setPending(false)
    }
  }

  return (
    <div>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        {FIELDS.map(({ key, label }) => (
          <label key={key} className="block">
            <span className="mb-1 block text-[10px] text-zinc-500">{label}</span>
            <input
              value={values[key]}
              onChange={set(key)}
              inputMode="decimal"
              aria-label={label}
              aria-invalid={Boolean(errors[key])}
              className={`w-full rounded-lg border bg-zinc-900 px-3 py-2 font-mono text-sm tabular-nums text-zinc-100 focus:outline-none ${
                errors[key] ? 'border-rose-500' : 'border-white/10 focus:border-violet-400/60'
              }`}
            />
            {errors[key] && <span className="mt-1 block text-xs text-rose-400">{errors[key]}</span>}
          </label>
        ))}
      </div>
      <div className="mt-4 flex items-center gap-3">
        <button
          type="button"
          disabled={!valid || pending}
          onClick={handleSave}
          className="rounded-lg border border-violet-400/50 bg-violet-400/10 px-3 py-1.5 text-xs text-violet-300 transition hover:bg-violet-400/20 disabled:opacity-40"
        >
          {pending ? '保存中…' : '保存风控参数'}
        </button>
        {savedAt && <span className="text-xs text-emerald-400">已保存 {savedAt}</span>}
        {saveError && <span className="text-xs text-rose-400">保存失败：{saveError}</span>}
      </div>
      <p className="mt-2 text-[10px] text-zinc-600">
        kill_switch 总开关请在顶栏操作；风控参数保存后下一轮决策生效。
      </p>
    </div>
  )
}
