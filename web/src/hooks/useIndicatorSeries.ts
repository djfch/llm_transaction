/**
 * 指标数据 hook：先拉短名单配置（getIndicatorConfig），再按完整短名单拉序列（getIndicatorSeries，
 * 前端始终显式传 keys——含 scalar 项：atr14/vol_ratio 有序列、oi 随响应返回 current，徽标才有数据）。
 * 失效信号：WS indicator_config_updated → 重拉配置（连带重拉序列）；
 * 当前合约 ticker → 节流重拉序列（复用后端按合约节流的 ticker 流，不开定时轮询）。
 * 防串数据：只消费 contract/interval 与当前一致的响应（切换合约/周期后旧响应直接隔离）。
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api'
import type { Candle, IndicatorConfig, IndicatorSeriesEntry } from '../api/types'
import {
  buildOverlaySeries,
  buildPaneSeries,
  displayName,
  scalarBadges,
  type OverlayLine,
  type PaneSpec,
  type ScalarBadge,
} from '../utils/indicatorSeries'
import { useApiData } from './useApiData'
import { useWs } from './useWs'

/** ticker 触发序列重拉的最小间隔（毫秒）：指标无需 tick 级新鲜度，避免频繁 setData 抖动 */
const TICKER_RELOAD_MIN_MS = 15_000

export interface UseIndicatorSeriesResult {
  overlays: OverlayLine[] // 主图叠加线（按 K 线 time 对齐）
  panes: PaneSpec[] // 副图配置（key + 系列）
  badges: ScalarBadge[] // scalar 徽标（oi/atr14 等当前值）
  shortlist: string[] // 短名单展示名（徽标条用，保持后端顺序）
  loading: boolean
  error: string | null
}

/** 完整短名单即请求键集（含 scalar：后端 series 端点对标量同样支持——atr/vol_ratio 给序列、oi 给 current） */
function seriesKeys(config: IndicatorConfig): string[] {
  return [...config.shortlist]
}

export function useIndicatorSeries(
  contract: string,
  interval: string,
  candles: Candle[],
  limit = 200,
  refreshKey = 0,
): UseIndicatorSeriesResult {
  // 短名单配置：indicator_config_updated 经 configTick 触发重拉
  const [configTick, setConfigTick] = useState(0)
  const configQ = useApiData(() => api.getIndicatorConfig(), [configTick])
  const config = configQ.data

  // 指标序列：依赖配置（keys 取自短名单），合约/周期/refreshKey 变化一并重拉
  const seriesQ = useApiData(
    () => {
      if (!config || contract === '') return Promise.resolve(null)
      const keys = seriesKeys(config)
      if (keys.length === 0) return Promise.resolve(null)
      return api.getIndicatorSeries(contract, interval, keys, limit)
    },
    [contract, interval, limit, config, refreshKey],
  )
  const { reload: reloadSeries } = seriesQ

  // 防串数据：useApiData 失败/加载中保留旧 data，只消费与当前合约+周期一致的响应
  const seriesData =
    seriesQ.data && seriesQ.data.contract === contract && seriesQ.data.interval === interval
      ? seriesQ.data
      : null

  // WS 失效：短名单变更重拉配置；当前合约 ticker 节流重拉序列
  const { lastMessage } = useWs()
  const lastTickerReloadRef = useRef(0)
  useEffect(() => {
    if (lastMessage?.type === 'indicator_config_updated') setConfigTick((t) => t + 1)
    if (lastMessage?.type === 'ticker' && lastMessage.data.contract === contract) {
      const now = Date.now()
      if (now - lastTickerReloadRef.current >= TICKER_RELOAD_MIN_MS) {
        lastTickerReloadRef.current = now
        reloadSeries()
      }
    }
  }, [lastMessage, contract, reloadSeries])

  // 短名单 overlay 项 → 主图线（按 K 线 time 对齐）
  const overlays = useMemo(() => {
    if (!config || !seriesData) return []
    const items: Array<{ key: string; entry: IndicatorSeriesEntry }> = []
    for (const key of config.shortlist) {
      const entry = seriesData.series[key]
      if (entry) items.push({ key, entry })
    }
    return buildOverlaySeries(items, candles)
  }, [config, seriesData, candles])

  // 短名单 pane 项 → 副图配置（保持短名单顺序，即副图上下顺序）
  const panes = useMemo(() => {
    if (!config || !seriesData) return []
    const out: PaneSpec[] = []
    for (const key of config.shortlist) {
      const entry = seriesData.series[key]
      if (entry?.kind === 'pane') out.push(buildPaneSeries(key, entry))
    }
    return out
  }, [config, seriesData])

  const badges = useMemo(() => (config ? scalarBadges(config, seriesData) : []), [config, seriesData])

  const shortlist = useMemo(() => {
    if (!config) return []
    return config.shortlist.map((key) => displayName(key, config.available.find((a) => a.key === key)?.label ?? ''))
  }, [config])

  return {
    overlays,
    panes,
    badges,
    shortlist,
    loading: configQ.loading || seriesQ.loading,
    error: configQ.error ?? seriesQ.error,
  }
}
