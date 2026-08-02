/**
 * K线买卖点标记映射：业务成交转换为圆形覆盖层使用的中间模型。
 * 语义：仅当前合约的成交生成标记；round_id 空串（历史/强平/止盈止损等无归属来源）
 * 同样保留（roundId 为 ''，覆盖层渲染为不可点击）；有归属的标记可点击定位决策轮。
 * size 正买负卖 —— 买 b（K线下方，绿）、卖 s（K线上方，红）。
 * 成交时间按周期归入 K 线；坐标与裁剪由覆盖层按当前图表范围计算。
 */
import type { Trade } from '../api/types'

/** 单个买卖点标记（业务字段，坐标由图表层换算后补充） */
export interface TradeMarker {
  id: number // 成交 ID（React key）
  timeSec: number // 成交时间（Unix 秒，与 K 线时间轴同口径）
  price: number // 成交价（找不到所属 bar 时的纵轴兜底）
  side: 'buy' | 'sell' // buy=买入成交(K线下方) sell=卖出成交(K线上方)
  roundId: string // 归属决策轮 ID（空串=无归属：标记可见但不可点击）
}

/** 成交时间（ISO 字符串）→ Unix 秒（向下取整）；非法时间返回 null（调用方跳过） */
export function tradeTimeSec(iso: string): number | null {
  const ms = new Date(iso).getTime()
  if (Number.isNaN(ms)) return null
  return Math.floor(ms / 1000)
}

/**
 * 成交列表 → 当前合约的买卖点标记（按时间升序）。
 * 过滤：非当前合约、时间非法；round_id 空串保留（roundId 为 ''，覆盖层渲染为不可点击）。
 */
export function buildTradeMarkers(trades: Trade[], contract: string): TradeMarker[] {
  const out: TradeMarker[] = []
  for (const t of trades) {
    if (t.contract !== contract) continue
    const timeSec = tradeTimeSec(t.time)
    if (timeSec === null) continue
    out.push({ id: t.id, timeSec, price: t.price, side: t.size > 0 ? 'buy' : 'sell', roundId: t.round_id })
  }
  return out.sort((a, b) => a.timeSec - b.timeSec)
}
