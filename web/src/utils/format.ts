/**
 * 展示格式化工具（纯函数，与业务解耦）。
 */

/** 数字千分位，保留 2 位小数 */
export function fmtNum(n: number, digits = 2): string {
  return n.toLocaleString('zh-CN', { minimumFractionDigits: digits, maximumFractionDigits: digits })
}

/** 带正负号的金额（盈亏用） */
export function fmtSigned(n: number, digits = 2): string {
  const sign = n > 0 ? '+' : ''
  return `${sign}${fmtNum(n, digits)}`
}

/** 整数或多位小数自适应价格 */
export function fmtPrice(n: number): string {
  return n >= 100 ? fmtNum(n, 2) : n.toLocaleString('zh-CN', { maximumFractionDigits: 6 })
}

/** ISO 时间 → 本地时间字符串 */
export function fmtTime(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString('zh-CN', { hour12: false })
}

/** 秒数 → 人类可读运行时长，如 "1天2小时3分" */
export function fmtUptime(seconds: number): string {
  const d = Math.floor(seconds / 86400)
  const h = Math.floor((seconds % 86400) / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  if (d > 0) return `${d}天${h}小时${m}分`
  if (h > 0) return `${h}小时${m}分`
  return `${m}分${Math.floor(seconds % 60)}秒`
}

/** 盈亏配色：正绿负红零灰 */
export function pnlClass(n: number): string {
  if (n > 0) return 'text-emerald-400'
  if (n < 0) return 'text-rose-400'
  return 'text-slate-400'
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
