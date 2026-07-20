/**
 * K线买卖点标记纯函数（与图表库解耦，供 MarkersOverlay 消费、单测直接覆盖）。
 * 语义：仅当前合约 + 已归属决策轮（round_id 非空）的成交才生成标记；
 * size 正买负卖 —— 买 b（K线下方，绿）、卖 s（K线上方，红）。
 * 坐标换算（timeToCoordinate/priceToCoordinate）由图表层完成，此处只产出业务字段。
 */
import type { Trade } from '../api/types'

/** 单个买卖点标记（业务字段，坐标由图表层换算后补充） */
export interface TradeMarker {
  id: number // 成交 ID（React key）
  timeSec: number // 成交时间（Unix 秒，与 K 线时间轴同口径）
  price: number // 成交价（找不到所属 bar 时的纵轴兜底）
  side: 'buy' | 'sell' // buy=买入/开多(K线下方) sell=卖出/平多(K线上方)
  roundId: string // 归属决策轮 ID（点击跳转定位用，保证非空）
}

/** 成交时间（ISO 字符串）→ Unix 秒（向下取整）；非法时间返回 null（调用方跳过） */
export function tradeTimeSec(iso: string): number | null {
  const ms = new Date(iso).getTime()
  if (Number.isNaN(ms)) return null
  return Math.floor(ms / 1000)
}

/**
 * 成交列表 → 当前合约的买卖点标记（按时间升序）。
 * 过滤：非当前合约、round_id 空串（历史/未知来源不渲染标记）、时间非法。
 */
export function buildTradeMarkers(trades: Trade[], contract: string): TradeMarker[] {
  const out: TradeMarker[] = []
  for (const t of trades) {
    if (t.contract !== contract || !t.round_id) continue
    const timeSec = tradeTimeSec(t.time)
    if (timeSec === null) continue
    out.push({ id: t.id, timeSec, price: t.price, side: t.size > 0 ? 'buy' : 'sell', roundId: t.round_id })
  }
  return out.sort((a, b) => a.timeSec - b.timeSec)
}
