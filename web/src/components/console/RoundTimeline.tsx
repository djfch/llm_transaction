/**
 * 决策时间线：决策轮卡片流（最新在前）。
 * 数据自管：getRounds(offset, 20) 分页「加载更多」追加（按 round_id 去重）；
 * WS round 事件仅作失效信号（payload 无 started_at/summary，见 api/types.ts 契约），
 * 收到后重拉第一页去重前合，新轮以完整数据置顶。
 * 定位响应：useRoundFocus().target 变化 → 已加载列表命中则展开+scrollIntoView+描边高亮 2s；
 * 未命中则逐页追加加载（上限 10 页）再找，仍无 → 顶部提示「未找到该决策轮」。
 * 笔记引文：挂载时 + WS round 事件时 getNotes()，建 round_id → 笔记 映射，归属卡片在 summary 下嵌引文。
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../../api'
import type { Note, RoundSummary } from '../../api/types'
import { useRoundFocus } from '../../hooks/useRoundFocus'
import { useWs } from '../../hooks/useWs'
import StateHint from '../StateHint'
import TimelineCard, { type RoundNote } from './TimelineCard'

/** 每页轮数 */
const PAGE_SIZE = 20
/** focus 定位时逐页加载的页数上限 */
const MAX_SEARCH_PAGES = 10

/** 分页追加合并：按 round_id 去重（与 WS 失效刷新前合互不冲突） */
function mergeAppend(prev: RoundSummary[], page: RoundSummary[]): RoundSummary[] {
  const seen = new Set(prev.map((r) => r.round_id))
  return [...prev, ...page.filter((r) => !seen.has(r.round_id))]
}

/** WS 失效刷新前合：重拉的第一页置顶（page 为完整口径，优先于本地同 id 旧数据） */
function mergePrepend(prev: RoundSummary[], page: RoundSummary[]): RoundSummary[] {
  const seen = new Set(page.map((r) => r.round_id))
  return [...page, ...prev.filter((r) => !seen.has(r.round_id))]
}

/** 笔记列表 → round_id 映射（getNotes 契约=最新在前，同轮多条首见即最新；空归属不入映射） */
function buildNotesMap(list: Note[]): Map<string, RoundNote> {
  const map = new Map<string, RoundNote>()
  for (const n of list) {
    if (n.round_id && !map.has(n.round_id)) map.set(n.round_id, { content: n.content, time: n.time })
  }
  return map
}

/**
 * focus 搜索：已加载未命中时逐页追加（上限 MAX_SEARCH_PAGES 页），返回最终是否命中。
 * fetchPage 负责取数并同步进组件 state；页不满即数据耗尽。
 */
async function searchRound(
  loaded: RoundSummary[],
  roundId: string,
  fetchPage: (offset: number) => Promise<RoundSummary[]>,
  isCancelled: () => boolean,
): Promise<boolean> {
  let list = loaded
  if (list.some((r) => r.round_id === roundId)) return true
  for (let pages = 0; pages < MAX_SEARCH_PAGES; pages += 1) {
    const page = await fetchPage(list.length)
    if (isCancelled() || page.length === 0) return false
    list = mergeAppend(list, page)
    if (list.some((r) => r.round_id === roundId)) return true
    if (page.length < PAGE_SIZE) return false
  }
  return false
}

export default function RoundTimeline() {
  const [rounds, setRounds] = useState<RoundSummary[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [loadingMore, setLoadingMore] = useState(false)
  const [moreError, setMoreError] = useState<string | null>(null)
  const [hasMore, setHasMore] = useState(true)
  const [expandedId, setExpandedId] = useState<string | null>(null)
  const [hlId, setHlId] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  // round_id → 归属笔记（卡片引文用；空 round_id 或无归属轮不入映射）
  const [notesMap, setNotesMap] = useState<ReadonlyMap<string, RoundNote>>(new Map())

  // rounds 的同步快照（focus 搜索读取，避免 effect 依赖列表数据反复触发）
  const roundsRef = useRef(rounds)
  roundsRef.current = rounds
  // 卡片锚点：round_id → article 元素（scrollIntoView 用）
  const cardRefs = useRef(new Map<string, HTMLElement>())

  // 挂载时拉一次笔记建 round_id 映射（契约=最新在前，同轮首见即最新）；
  // 引文为锦上添花：失败静默兜底，卡片照常渲染
  useEffect(() => {
    let alive = true
    api
      .getNotes()
      .then((list) => {
        if (alive) setNotesMap(buildNotesMap(list))
      })
      .catch(() => {})
    return () => {
      alive = false
    }
  }, [])

  /** 取一页并追加进列表（首屏/加载更多/focus 搜索共用） */
  const fetchAndAppend = useCallback(async (offset: number): Promise<RoundSummary[]> => {
    const page = await api.getRounds(offset, PAGE_SIZE)
    setRounds((prev) => mergeAppend(prev, page))
    setHasMore(page.length === PAGE_SIZE)
    return page
  }, [])

  // 首屏加载第一页
  useEffect(() => {
    let alive = true
    fetchAndAppend(0)
      .catch((e: unknown) => {
        if (alive) setError(e instanceof Error ? e.message : String(e))
      })
      .finally(() => {
        if (alive) setLoading(false)
      })
    return () => {
      alive = false
    }
  }, [fetchAndAppend])

  // WS round 事件：payload 仅 {round_id, ok, wake_source}（契约见 api/types.ts），不作数据源，
  // 只当失效信号——重拉第一页去重前合（新轮以完整数据置顶；空 round_id 幽灵轮不入库，天然免疫）；
  // 同步重拉笔记映射（新轮可能带来新归属笔记）
  const { lastMessage } = useWs()
  useEffect(() => {
    if (lastMessage?.type !== 'round') return
    let alive = true
    api
      .getRounds(0, PAGE_SIZE)
      .then((page) => {
        if (alive) setRounds((prev) => mergePrepend(prev, page))
      })
      .catch(() => {}) // 刷新失败静默兜底：列表保持现状，待下次事件/手动刷新
    api
      .getNotes()
      .then((list) => {
        if (alive) setNotesMap(buildNotesMap(list))
      })
      .catch(() => {}) // 引文失败静默兜底：保持旧映射
    return () => {
      alive = false
    }
  }, [lastMessage])

  /** 命中后：展开 + 滚动到卡片中央 + 描边高亮 2s */
  const reveal = useCallback((roundId: string) => {
    setExpandedId(roundId)
    setHlId(roundId)
    // 等展开渲染完成再滚动（jsdom 无 scrollIntoView，测试注入桩）
    setTimeout(() => {
      cardRefs.current.get(roundId)?.scrollIntoView({ behavior: 'smooth', block: 'center' })
    }, 50)
    setTimeout(() => setHlId((cur) => (cur === roundId ? null : cur)), 2000)
  }, [])

  // focus 定位：已加载命中直接 reveal；否则逐页加载再找；仍无 → 顶部提示
  const { target } = useRoundFocus()
  useEffect(() => {
    if (!target) return
    let cancelled = false
    const run = async () => {
      setNotice(null)
      const found = await searchRound(roundsRef.current, target.roundId, fetchAndAppend, () => cancelled).catch(
        () => false, // 搜索中途取数失败：按未命中处理（提示兜底）
      )
      if (cancelled) return
      if (found) reveal(target.roundId)
      else setNotice(`未找到该决策轮：${target.roundId}`)
    }
    void run()
    return () => {
      cancelled = true
    }
  }, [target, fetchAndAppend, reveal])

  const onLoadMore = () => {
    setLoadingMore(true)
    setMoreError(null)
    fetchAndAppend(roundsRef.current.length)
      .catch((e: unknown) => setMoreError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoadingMore(false))
  }

  const cardRefOf = (roundId: string) => (el: HTMLElement | null) => {
    if (el) cardRefs.current.set(roundId, el)
    else cardRefs.current.delete(roundId)
  }

  return (
    <section className="rounded-xl border border-zinc-800 bg-zinc-950/80 p-5 shadow-lg shadow-black/30">
      <header className="mb-4">
        <h2 className="text-sm font-semibold text-zinc-200">
          决策时间线 <span className="text-xs font-normal text-zinc-500">— Agent 的每一笔思考都留痕</span>
        </h2>
      </header>

      {notice && (
        <div
          role="alert"
          className="mb-3 flex items-center gap-2 rounded-lg border border-amber-400/40 bg-amber-400/10 px-3 py-2 text-xs text-amber-300"
        >
          {notice}
          <button
            type="button"
            onClick={() => setNotice(null)}
            className="ml-auto text-amber-300/70 transition hover:text-amber-200"
          >
            ✕
          </button>
        </div>
      )}

      <StateHint loading={loading} error={error} empty={rounds.length === 0}>
        <ol className="space-y-3">
          {rounds.map((r) => (
            <li key={r.round_id}>
              <TimelineCard
                round={r}
                note={notesMap.get(r.round_id)}
                expanded={expandedId === r.round_id}
                highlight={hlId === r.round_id}
                onToggle={() => setExpandedId((cur) => (cur === r.round_id ? null : r.round_id))}
                cardRef={cardRefOf(r.round_id)}
              />
            </li>
          ))}
        </ol>

        <footer className="mt-4 border-t border-zinc-800/80 pt-3 text-center">
          {hasMore && (
            <button
              type="button"
              onClick={onLoadMore}
              disabled={loadingMore}
              className="rounded-lg border border-zinc-700 bg-zinc-900 px-4 py-1.5 text-xs text-zinc-300 transition hover:border-violet-400/50 hover:text-violet-300 disabled:opacity-50"
            >
              {loadingMore ? '加载中…' : '加载更多'}
            </button>
          )}
          {moreError && <p className="mt-2 text-xs text-rose-400">加载失败：{moreError}</p>}
        </footer>
      </StateHint>
    </section>
  )
}
