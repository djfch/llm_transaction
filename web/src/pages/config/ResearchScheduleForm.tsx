/** 研报自动执行表单：管理总开关、三个市场预设和 UTC+8 自定义时间。 */
import { useMemo, useState } from 'react'
import type {
  FixedTimeResearchSchedule,
  MarketOpenResearchSchedule,
  ResearchCalendarCode,
  ResearchScheduleConfig,
  ResearchSchedule,
  ResearchScheduleStatus,
} from '../../api/types'

const inputCls =
  'rounded-lg border border-white/10 bg-zinc-900 px-2.5 py-2 text-xs text-zinc-100 focus:border-violet-400/60 focus:outline-none'
const PRESET_META = {
  asia_open: { title: '亚盘 · 东京', rule: '东京 09:00 开盘前 30 分钟' },
  europe_open: { title: '欧盘 · 伦敦', rule: '伦敦 08:00 开盘前 30 分钟' },
  us_open: { title: '美盘 · 纽约', rule: '纽约 09:30 开盘前 30 分钟' },
} as const
const CALENDAR_LABELS: Record<ResearchCalendarCode, string> = {
  daily: '每天',
  XTKS: '东京交易日',
  XLON: '伦敦交易日',
  XNYS: '纽约交易日',
}
const PRESET_TIMES = {
  asia_open: ['07:30'],
  europe_open: ['14:30', '15:30'],
  us_open: ['21:00', '22:00'],
} as const

/** 复制配置，避免表单直接修改查询缓存。 */
function cloneConfig(config: ResearchScheduleConfig): ResearchScheduleConfig {
  return structuredClone(config)
}

/** 生成后端可校验的 UUID；旧浏览器缺少 randomUUID 时使用随机模板。 */
function newScheduleId(): string {
  if (typeof crypto.randomUUID === 'function') return crypto.randomUUID()
  return '10000000-1000-4000-8000-100000000000'.replace(/[018]/g, (char) =>
    (Number(char) ^ (crypto.getRandomValues(new Uint8Array(1))[0] & (15 >> (Number(char) / 4)))).toString(16),
  )
}

/** 把 Unix 秒格式化为明确的 UTC+8 日期时间。 */
function formatNextRun(value: number | null | undefined): string {
  if (value == null) return '暂无'
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(new Date(value * 1000))
}

/** 保存前校验自定义时间格式、重复项和预设冲突。 */
function validateSchedules(schedules: ResearchSchedule[]): string | null {
  const activeTimes = new Set<string>()
  const blockedTimes = new Set<string>(
    schedules
      .filter(
        (item): item is MarketOpenResearchSchedule =>
          item.kind === 'market_open' && item.enabled,
      )
      .flatMap((item) => PRESET_TIMES[item.id]),
  )
  for (const item of schedules) {
    if (item.kind !== 'fixed_time' || !item.enabled) continue
    if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(item.time)) return `时间 ${item.time} 格式无效`
    if (activeTimes.has(item.time)) return `启用的自定义时间 ${item.time} 重复`
    if (blockedTimes.has(item.time)) return `${item.time} 可能与市场预设冲突`
    activeTimes.add(item.time)
  }
  return null
}

/** 读取指定调度项的下一次执行时间。 */
function nextRun(status: ResearchScheduleStatus | null, id: string): string {
  return formatNextRun(status?.items.find((item) => item.id === id)?.next_run_at)
}

/** 单个市场预设卡片。 */
function PresetCard({
  item,
  status,
  onToggle,
}: {
  item: Extract<ResearchSchedule, { kind: 'market_open' }>
  status: ResearchScheduleStatus | null
  onToggle: (enabled: boolean) => void
}) {
  const meta = PRESET_META[item.id]
  return (
    <article className="rounded-xl border border-white/10 bg-white/[0.025] p-3">
      <div className="flex items-start gap-3">
        <div className="min-w-0 flex-1">
          <h4 className="text-sm font-medium text-zinc-200">{meta.title}</h4>
          <p className="mt-1 text-[11px] text-zinc-500">{meta.rule}</p>
          <p className="mt-2 text-[11px] text-zinc-400">下次：{nextRun(status, item.id)}（UTC+8）</p>
        </div>
        <input
          type="checkbox"
          aria-label={`启用${meta.title.split(' · ')[0]}调度`}
          checked={item.enabled}
          onChange={(event) => onToggle(event.target.checked)}
          className="mt-1 h-4 w-4 accent-violet-500"
        />
      </div>
    </article>
  )
}

/** 研报自动执行配置主表单。 */
export default function ResearchScheduleForm({
  initial,
  status,
  onSave,
}: {
  initial: ResearchScheduleConfig
  status: ResearchScheduleStatus | null
  onSave: (research: ResearchScheduleConfig) => Promise<void>
}) {
  const [form, setForm] = useState(() => cloneConfig(initial))
  const [pending, setPending] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const presets = useMemo(() => form.schedules.filter((item) => item.kind === 'market_open'), [form])
  const custom = useMemo(() => form.schedules.filter((item) => item.kind === 'fixed_time'), [form])

  /** 更新单个调度项并清除旧保存状态。 */
  const patchItem = (id: string, patch: Partial<FixedTimeResearchSchedule> | { enabled: boolean }) => {
    setForm((current) => ({
      ...current,
      schedules: current.schedules.map((item) => (item.id === id ? { ...item, ...patch } as ResearchSchedule : item)),
    }))
    setMessage(null)
    setError(null)
  }

  /** 添加一个不与预设冲突的默认自定义时间。 */
  const addCustom = () => {
    setForm((current) => ({
      ...current,
      schedules: [
        ...current.schedules,
        { id: newScheduleId(), kind: 'fixed_time', enabled: true, time: '12:00', calendar: 'daily' },
      ],
    }))
    setMessage(null)
    setError(null)
  }

  /** 删除自定义项并清除旧校验提示。 */
  const removeCustom = (id: string) => {
    setForm((current) => ({
      ...current,
      schedules: current.schedules.filter((entry) => entry.id !== id),
    }))
    setMessage(null)
    setError(null)
  }

  /** 校验并保存完整 research 段。 */
  const save = async () => {
    const validationError = validateSchedules(form.schedules)
    if (validationError) {
      setError(validationError)
      return
    }
    setPending(true)
    setError(null)
    try {
      await onSave(cloneConfig(form))
      setMessage('已生效')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setPending(false)
    }
  }

  return (
    <div className="space-y-4">
      <label className="flex items-start gap-3 rounded-xl border border-violet-400/20 bg-violet-400/5 p-3">
        <input
          type="checkbox"
          aria-label="启用自动研报"
          checked={form.enabled}
          onChange={(event) => {
            setForm((current) => ({ ...current, enabled: event.target.checked }))
            setMessage(null)
            setError(null)
          }}
          className="mt-0.5 h-4 w-4 accent-violet-500"
        />
        <span>
          <span className="block text-sm text-zinc-200">自动研报总开关</span>
          <span className="mt-1 block text-[11px] text-zinc-500">关闭后保留全部时间设置，手动生成不受影响</span>
        </span>
      </label>

      {status?.calendar.warning && (
        <p role="alert" className="rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-300">
          日历降级：{status.calendar.warning}
        </p>
      )}

      <div className="grid gap-2">
        {presets.map((item) => (
          <PresetCard key={item.id} item={item} status={status} onToggle={(enabled) => patchItem(item.id, { enabled })} />
        ))}
      </div>

      <div className="space-y-2">
        {custom.map((item, index) => (
          <div key={item.id} className="grid grid-cols-[auto_1fr_1.4fr_auto] items-center gap-2 rounded-xl border border-white/10 p-2.5">
            <input
              type="checkbox"
              aria-label={`启用自定义时间 ${index + 1}`}
              checked={item.enabled}
              onChange={(event) => patchItem(item.id, { enabled: event.target.checked })}
              className="h-4 w-4 accent-violet-500"
            />
            <input
              type="time"
              aria-label={`自定义执行时间 ${index + 1}`}
              value={item.time}
              onChange={(event) => patchItem(item.id, { time: event.target.value })}
              className={inputCls}
            />
            <select
              aria-label={`自定义日期规则 ${index + 1}`}
              value={item.calendar}
              onChange={(event) => patchItem(item.id, { calendar: event.target.value as ResearchCalendarCode })}
              className={inputCls}
            >
              {Object.entries(CALENDAR_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
            </select>
            <button
              type="button"
              aria-label={`删除自定义时间 ${index + 1}`}
              onClick={() => removeCustom(item.id)}
              className="px-1 text-zinc-500 transition hover:text-rose-300"
            >
              ✕
            </button>
            <p className="col-start-2 col-span-2 text-[10px] text-zinc-500">下次：{nextRun(status, item.id)}（UTC+8）</p>
          </div>
        ))}
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <button type="button" onClick={addCustom} className="rounded-lg border border-white/10 px-3 py-1.5 text-xs text-zinc-300 hover:bg-white/5">
          添加时间点
        </button>
        <button type="button" disabled={pending} onClick={save} className="rounded-lg border border-violet-400/50 bg-violet-400/10 px-3 py-1.5 text-xs text-violet-300 hover:bg-violet-400/20 disabled:opacity-40">
          {pending ? '保存中…' : '保存研报设置'}
        </button>
        {message && <span className="text-xs text-emerald-400">{message}</span>}
        {error && <span role="alert" className="text-xs text-rose-400">保存失败：{error}</span>}
      </div>
    </div>
  )
}
