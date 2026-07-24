/**
 * 决策时间线：服务端分页展示决策轮；卡片详情按需加载，笔记引文独立读取最新内容。
 * WebSocket round 事件只作失效信号，当前页和总数均以 REST 响应为准。
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../../api'
import type { Note, RoundSummary } from '../../api/types'
import { useApiData } from '../../hooks/useApiData'
import { usePageState } from '../../hooks/usePageState'
import { useRoundFocus } from '../../hooks/useRoundFocus'
import { useWs } from '../../hooks/useWs'
import StateHint from '../StateHint'
import PaginationControls from './PaginationControls'
import TimelineCard, { type RoundNote } from './TimelineCard'

/** 决策时间线每页条数，由已确认的交互规范固定为 5。 */
const PAGE_SIZE = 5
/** 笔记引文读取最近条数，保留原有最新 20 条的展示口径。 */
const NOTE_QUOTE_LIMIT = 20
/** 空决策页复用同一引用，避免 effect 依赖因新建空数组而反复变化。 */
const EMPTY_ROUNDS: RoundSummary[] = []

/** 笔记列表转换为 round_id(决策轮 ID) 到最新引文的映射。 */
function buildNotesMap(list: Note[]): Map<string, RoundNote> {
  const map = new Map<string, RoundNote>()
  for (const note of list) {
    if (note.round_id && !map.has(note.round_id)) {
      map.set(note.round_id, { content: note.content, time: note.time })
    }
  }
  return map
}

/** 读取时间线卡片使用的最新笔记引文，失败由调用方静默保持旧映射。 */
async function fetchNotesMap(): Promise<Map<string, RoundNote>> {
  const page = await api.getNotes(0, NOTE_QUOTE_LIMIT)
  return buildNotesMap(page.items)
}

/** 查找完整历史中的目标决策轮，命中时返回其零基页码。 */
async function findRoundPage(
  roundId: string,
  currentPage: number,
  currentItems: RoundSummary[],
  totalPages: number,
  isCancelled: () => boolean,
): Promise<number | null> {
  if (currentItems.some((round) => round.round_id === roundId)) return currentPage
  for (let page = 0; page < totalPages; page += 1) {
    if (page === currentPage) continue
    const result = await api.getRounds(page * PAGE_SIZE, PAGE_SIZE)
    if (isCancelled()) return null
    if (result.items.some((round) => round.round_id === roundId)) return page
  }
  return null
}

/** 分页展示决策轮，并保留外部焦点定位、卡片展开与笔记引文能力。 */
export default function RoundTimeline() {
  const [total, setTotal] = useState(0)
  const [expandedId, setExpandedId] = useState<string | null>(null)
  const [highlightId, setHighlightId] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [pendingFocusId, setPendingFocusId] = useState<string | null>(null)
  const [notesMap, setNotesMap] = useState<ReadonlyMap<string, RoundNote>>(new Map())
  const { page, totalPages, goToPage } = usePageState(total, PAGE_SIZE)
  const query = useApiData(
    () => api.getRounds(page * PAGE_SIZE, PAGE_SIZE),
    [page],
  )
  const { lastMessage } = useWs()
  const { target } = useRoundFocus()
  const cardRefs = useRef(new Map<string, HTMLElement>())
  const items = query.data?.items ?? EMPTY_ROUNDS
  const { reload } = query

  useEffect(() => {
    if (query.data) setTotal(query.data.total)
  }, [query.data])

  useEffect(() => {
    let alive = true
    fetchNotesMap()
      .then((map) => {
        if (alive) setNotesMap(map)
      })
      .catch(() => {})
    return () => {
      alive = false
    }
  }, [])

  useEffect(() => {
    if (lastMessage?.type !== 'round') return
    let alive = true
    reload()
    fetchNotesMap()
      .then((map) => {
        if (alive) setNotesMap(map)
      })
      .catch(() => {})
    return () => {
      alive = false
    }
  }, [lastMessage, reload])

  /** 展开目标卡片、滚动到可视区中央，并短暂显示定位高亮。 */
  const reveal = useCallback((roundId: string) => {
    setExpandedId(roundId)
    setHighlightId(roundId)
    setTimeout(() => {
      cardRefs.current.get(roundId)?.scrollIntoView({ behavior: 'smooth', block: 'center' })
    }, 50)
    setTimeout(() => setHighlightId((current) => (current === roundId ? null : current)), 2000)
  }, [])

  /** 切换页面时收起旧页展开项，避免详情状态误带到新页。 */
  const changePage = useCallback(
    (nextPage: number) => {
      if (nextPage === page) return
      setExpandedId(null)
      setHighlightId(null)
      goToPage(nextPage)
    },
    [goToPage, page],
  )

  useEffect(() => {
    if (!pendingFocusId || !items.some((round) => round.round_id === pendingFocusId)) return
    reveal(pendingFocusId)
    setPendingFocusId(null)
  }, [items, pendingFocusId, reveal])

  useEffect(() => {
    // 首屏尚未返回 total(总数) 和 items(当前页内容) 时不能判定目标不存在。
    if (!target || pendingFocusId || query.loading || !query.data) return
    let cancelled = false
    const focusRound = async () => {
      setNotice(null)
      const foundPage = await findRoundPage(
        target.roundId,
        page,
        items,
        totalPages,
        () => cancelled,
      ).catch(() => null)
      if (cancelled) return
      if (foundPage === null) {
        setNotice(`未找到该决策轮：${target.roundId}`)
      } else if (foundPage === page) {
        reveal(target.roundId)
      } else {
        setPendingFocusId(target.roundId)
        changePage(foundPage)
      }
    }
    void focusRound()
    return () => {
      cancelled = true
    }
  }, [target, pendingFocusId, page, totalPages, items, query.loading, query.data, reveal, changePage])

  /** 维护 round_id 到卡片元素的引用，供 reveal 的平滑滚动使用。 */
  const cardRefOf = (roundId: string) => (element: HTMLElement | null) => {
    if (element) cardRefs.current.set(roundId, element)
    else cardRefs.current.delete(roundId)
  }

  return (
    <section className="rounded-xl border border-zinc-800 bg-zinc-950/80 p-5 shadow-lg shadow-black/30">
      <header className="mb-4 flex flex-wrap items-center gap-2">
        <h2 className="text-sm font-semibold text-zinc-200">
          决策时间线 <span className="text-xs font-normal text-zinc-500">— Agent 的每一笔思考都留痕</span>
        </h2>
        <span className="ml-auto text-xs tabular-nums text-zinc-500">共 {total} 条决策</span>
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

      <StateHint loading={query.loading} error={query.error} empty={total === 0}>
        <ol className="space-y-3">
          {items.map((round) => (
            <li key={round.round_id}>
              <TimelineCard
                round={round}
                note={notesMap.get(round.round_id)}
                expanded={expandedId === round.round_id}
                highlight={highlightId === round.round_id}
                onToggle={() => setExpandedId((current) => (current === round.round_id ? null : round.round_id))}
                cardRef={cardRefOf(round.round_id)}
              />
            </li>
          ))}
        </ol>
        <PaginationControls
          page={page}
          total={total}
          pageSize={PAGE_SIZE}
          itemLabel="决策"
          loading={query.loading}
          onPageChange={changePage}
        />
      </StateHint>
    </section>
  )
}
