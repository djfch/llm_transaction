/**
 * 展示格式化工具（纯函数，与业务解耦）。
 * 全部函数对 undefined/NaN 防御：后端字段缺失时显示 '-' 而不是让整页渲染崩溃。
 */

/** 数字千分位，保留 2 位小数；空值显示 '-' */
export function fmtNum(n: number, digits = 2): string {
  if (n == null || Number.isNaN(n)) return '-'
  return n.toLocaleString('zh-CN', { minimumFractionDigits: digits, maximumFractionDigits: digits })
}

/** 带正负号的金额（盈亏用） */
export function fmtSigned(n: number, digits = 2): string {
  const sign = n > 0 ? '+' : ''
  return `${sign}${fmtNum(n, digits)}`
}

/** 整数或多位小数自适应价格；空值显示 '-' */
export function fmtPrice(n: number): string {
  if (n == null || Number.isNaN(n)) return '-'
  return n >= 100 ? fmtNum(n, 2) : n.toLocaleString('zh-CN', { maximumFractionDigits: 6 })
}

/** ISO 时间 → 本地时间字符串 */
export function fmtTime(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString('zh-CN', { hour12: false })
}

/** 秒数 → 人类可读运行时长，如 "1天2小时3分4秒" */
export function fmtUptime(seconds: number): string {
  const d = Math.floor(seconds / 86400)
  const h = Math.floor((seconds % 86400) / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  const s = Math.floor(seconds % 60)
  if (d > 0) return `${d}天${h}小时${m}分${s}秒`
  if (h > 0) return `${h}小时${m}分${s}秒`
  return `${m}分${s}秒`
}

/** 盈亏配色：正绿负红零灰 */
export function pnlClass(n: number): string {
  if (n > 0) return 'text-emerald-400'
  if (n < 0) return 'text-rose-400'
  return 'text-slate-400'
}

/** 0-1 比例 → 紧凑百分比（去掉多余 0）：0.3 → "30%"、0.005 → "0.5%"；空值显示 '-' */
export function fmtPct(ratio: number): string {
  if (ratio == null || Number.isNaN(ratio)) return '-'
  return `${(ratio * 100).toFixed(2).replace(/\.?0+$/, '')}%`
}

/** 0-1 比例 → 固定两位小数百分比：0.10351 → "10.35%"；空值显示 '-' */
export function fmtPct2(ratio: number): string {
  if (ratio == null || Number.isNaN(ratio)) return '-'
  return `${(ratio * 100).toFixed(2)}%`
}

/** 带正负号的百分比（收益率类）：0.1108 → "+11.08%"；空值显示 '-' */
export function fmtSignedPct(ratio: number): string {
  if (ratio == null || Number.isNaN(ratio)) return '-'
  const sign = ratio > 0 ? '+' : ''
  return `${sign}${(ratio * 100).toFixed(2)}%`
}

/** ISO 时间 → 本地 HH:MM（笔记引文等小尺寸场景）；不可解析时原样返回 */
export function fmtClock(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false })
}

/** round_id → 展示用短号：uuid 取前 8 位；含分隔符的 id（如 mock 的 round-0037）取末段，避免截断雷同 */
export function shortRoundId(roundId: string): string {
  if (!roundId) return ''
  const seg = roundId.includes('-') ? (roundId.split('-').pop() ?? roundId) : roundId
  return seg.length > 8 ? seg.slice(0, 8) : seg
}

/** source(成交来源) → 徽标文案与色调（与 Badge 的 tone 对齐） */
export function sourceBadge(source: string): {
  text: string
  tone: 'ok' | 'danger' | 'warn' | 'neutral' | 'info'
} {
  switch (source) {
    case 'llm_open':
      return { text: 'LLM开仓', tone: 'info' }
    case 'llm_close':
      return { text: 'LLM平仓', tone: 'warn' }
    case 'user_close':
      return { text: '用户平仓', tone: 'danger' }
    case 'liquidation':
      return { text: '强平', tone: 'danger' }
    case 'tpsl_close':
      return { text: '止盈止损', tone: 'warn' }
    default:
      return { text: '-', tone: 'neutral' }
  }
}

/** created_by(策略版本来源) → 中文文案：human→人工 / review_agent→复盘 / rollback→回滚；未知值原样显示 */
export function strategyCreatorText(createdBy: string): string {
  switch (createdBy) {
    case 'human':
      return '人工'
    case 'review_agent':
      return '复盘'
    case 'rollback':
      return '回滚'
    default:
      return createdBy
  }
}

/** created_by(策略版本来源) → 徽标样式：复盘紫 / 回滚青 / 人工及其他灰（StrategyVersions 与 StrategyPanel 共用） */
export function strategyCreatorBadgeClass(createdBy: string): string {
  const base = 'rounded border px-1.5 py-0.5 text-[10px] font-medium'
  switch (createdBy) {
    case 'review_agent':
      return `${base} border-violet-400/40 bg-violet-400/10 text-violet-300`
    case 'rollback':
      return `${base} border-cyan-400/40 bg-cyan-400/10 text-cyan-300`
    default:
      return `${base} border-zinc-600/50 bg-zinc-700/30 text-zinc-400`
  }
}
