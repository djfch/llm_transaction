/**
 * 风控参数表单的纯校验逻辑（与组件解耦，便于单测）。
 * 所有字段以字符串录入，此处统一做范围/格式校验与数值解析。
 */
import type { RiskConfig } from '../../api/types'

/** 表单字符串值（kill_switch 不在表单内编辑，走独立开关） */
export interface RiskFormValues {
  max_position_pct: string // 单仓占比上限
  max_total_position_pct: string // 总仓占比上限
  max_position_stop_risk_pct: string // 单仓整仓计划止损风险上限
  max_leverage: string // 杠杆上限
  daily_loss_limit: string // 日亏损锁仓阈值
  max_orders_per_day: string // 日下单上限
  max_deviation: string // 价格偏离保护
}

export type RiskFormErrors = Partial<Record<keyof RiskFormValues, string>>

/** 解析为数字，非法返回 NaN */
function num(v: string): number {
  return v.trim() === '' ? NaN : Number(v)
}

function isInt(v: string): boolean {
  return /^-?\d+$/.test(v.trim())
}

/** 逐字段校验，返回字段 → 错误文案 的映射（空对象表示通过） */
export function validateRisk(values: RiskFormValues): RiskFormErrors {
  const errors: RiskFormErrors = {}

  const single = num(values.max_position_pct)
  if (!(single > 0 && single <= 1)) errors.max_position_pct = '单仓占比需在 (0, 1] 区间'

  const total = num(values.max_total_position_pct)
  if (!(total > 0 && total <= 1)) {
    errors.max_total_position_pct = '总仓占比需在 (0, 1] 区间'
  } else if (!Number.isNaN(single) && total < single) {
    errors.max_total_position_pct = '总仓上限不能小于单仓上限'
  }

  const stopRisk = num(values.max_position_stop_risk_pct)
  if (!(stopRisk > 0 && stopRisk <= 1)) {
    errors.max_position_stop_risk_pct = '单仓计划止损风险上限需在 (0, 1] 区间'
  }

  if (!isInt(values.max_leverage) || num(values.max_leverage) < 1 || num(values.max_leverage) > 100) {
    errors.max_leverage = '杠杆上限需为 1–100 的整数'
  }

  const loss = num(values.daily_loss_limit)
  if (!(loss > 0 && loss <= 1)) errors.daily_loss_limit = '日亏损阈值需在 (0, 1] 区间'

  if (
    !isInt(values.max_orders_per_day) ||
    num(values.max_orders_per_day) < 1 ||
    num(values.max_orders_per_day) > 1000
  ) {
    errors.max_orders_per_day = '日下单上限需为 1–1000 的整数'
  }

  const dev = num(values.max_deviation)
  if (!(dev > 0 && dev <= 1)) errors.max_deviation = '价格偏离上限需在 (0, 1] 区间'

  return errors
}

/** 校验通过时解析为数值对象，否则返回 null */
export function parseRisk(values: RiskFormValues): Omit<RiskConfig, 'kill_switch'> | null {
  if (Object.keys(validateRisk(values)).length > 0) return null
  return {
    max_position_pct: num(values.max_position_pct),
    max_total_position_pct: num(values.max_total_position_pct),
    max_position_stop_risk_pct: num(values.max_position_stop_risk_pct),
    max_leverage: num(values.max_leverage),
    daily_loss_limit: num(values.daily_loss_limit),
    max_orders_per_day: num(values.max_orders_per_day),
    max_deviation: num(values.max_deviation),
  }
}
