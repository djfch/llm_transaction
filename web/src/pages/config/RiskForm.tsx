/**
 * 风控参数表单：字符串录入 + 即时校验，保存时合并回 RiskConfig（保留 kill_switch）。
 */
import { useMemo, useState } from 'react'
import type { RiskConfig } from '../../api/types'
import { parseRisk, validateRisk, type RiskFormValues } from './validate'

/** 字段元数据：key + `变量名(含义)` 标签 */
const FIELDS: Array<{ key: keyof RiskFormValues; label: string }> = [
  { key: 'max_position_pct', label: 'max_position_pct(单仓占比上限)' },
  { key: 'max_total_position_pct', label: 'max_total_position_pct(总仓占比上限)' },
  { key: 'max_leverage', label: 'max_leverage(杠杆上限)' },
  { key: 'daily_loss_limit', label: 'daily_loss_limit(日亏损锁仓阈值)' },
  { key: 'max_orders_per_day', label: 'max_orders_per_day(日下单上限)' },
  { key: 'max_deviation', label: 'max_deviation(价格偏离保护)' },
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
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {FIELDS.map(({ key, label }) => (
          <label key={key} className="block text-xs">
            <span className="mb-1 block text-slate-400">{label}</span>
            <input
              value={values[key]}
              onChange={set(key)}
              inputMode="decimal"
              aria-label={label}
              aria-invalid={Boolean(errors[key])}
              className={`w-full rounded-lg border bg-slate-800 px-3 py-2 text-sm tabular-nums text-slate-100 focus:outline-none ${
                errors[key] ? 'border-rose-500' : 'border-slate-700 focus:border-sky-500'
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
          className="rounded-lg bg-sky-600 px-4 py-2 text-sm font-medium text-white hover:bg-sky-500 disabled:opacity-40"
        >
          {pending ? '保存中…' : '保存风控参数'}
        </button>
        {savedAt && <span className="text-xs text-emerald-400">已保存 {savedAt}</span>}
        {saveError && <span className="text-xs text-rose-400">保存失败：{saveError}</span>}
      </div>
      <p className="mt-2 text-xs text-slate-500">
        kill_switch(总开关) 请在仪表盘操作；风控参数保存后下一轮决策生效。
      </p>
    </div>
  )
}
