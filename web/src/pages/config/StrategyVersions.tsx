/**
 * 策略版本历史：版本列表（vN / 来源徽标 / reason / 时间 / 是否当前）+ 点选两版本看 diff + 回滚。
 * 「当前」= md5 与最新版本一致的首项（版本表最新在前；回滚同文会产生重复 md5，取最新者）。
 * diff 方向固定 旧 → 新（from=min(id)，to=max(id)），行首 + 绿 / - 红着色（+++/-- 头部行灰显）。
 * 回滚：window.confirm 确认 → rollbackStrategy → 提示成功并刷新版本列表，
 * 同时经 onRolledBack 通知宿主重拉策略全文（编辑器内容由宿主协调刷新）。
 * 版本列表查询自管；挂在 StrategyEditor 所在 DrawerSection 内（其下方）。
 */
import { useEffect, useState } from 'react'
import { api } from '../../api'
import type { StrategyVersion } from '../../api/types'
import { useApiData } from '../../hooks/useApiData'
import { fmtTime, strategyCreatorText } from '../../utils/format'
import StateHint from '../../components/StateHint'

/** 空版本列表复用同一引用。 */
const EMPTY_VERSIONS: StrategyVersion[] = []

/** created_by 徽标样式：复盘紫 / 回滚青 / 人工及其他灰 */
function creatorBadgeClass(createdBy: string): string {
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

/** diff 行着色：+ 绿 / - 红；+++ 与 --- 文件头行灰显，上下文行暗灰 */
function diffLineClass(line: string): string {
  if (line.startsWith('+++') || line.startsWith('---')) return 'text-zinc-500'
  if (line.startsWith('+')) return 'text-emerald-300'
  if (line.startsWith('-')) return 'text-rose-300'
  return 'text-zinc-400'
}

/** 单版本行：点击切换 diff 选择（最多两个）；非当前版本提供「回滚到此版本」 */
function VersionRow({
  version,
  isCurrent,
  selected,
  rolling,
  onToggle,
  onRollback,
}: {
  version: StrategyVersion
  isCurrent: boolean
  selected: boolean
  rolling: boolean
  onToggle: () => void
  onRollback: () => void
}) {
  return (
    <li
      onClick={onToggle}
      className={`cursor-pointer rounded-lg border px-3 py-2 transition ${
        selected ? 'border-violet-400/60 bg-violet-400/[.07]' : 'border-zinc-800 bg-zinc-900/60 hover:border-zinc-700'
      }`}
      title={selected ? '点击取消 diff 选择' : '点击加入 diff 对比'}
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-xs font-bold text-zinc-200">v{version.id}</span>
        <span className={creatorBadgeClass(version.createdBy)}>{strategyCreatorText(version.createdBy)}</span>
        {isCurrent && (
          <span className="rounded border border-emerald-400/40 bg-emerald-400/10 px-1.5 py-0.5 text-[10px] font-medium text-emerald-300">
            当前
          </span>
        )}
        <span className="ml-auto font-mono text-[10px] tabular-nums text-zinc-500">{fmtTime(version.time)}</span>
        {!isCurrent && (
          <button
            type="button"
            disabled={rolling}
            onClick={(e) => {
              e.stopPropagation() // 回滚是写操作，不触发行选择的切换
              onRollback()
            }}
            className="rounded border border-amber-400/40 bg-amber-400/10 px-2 py-0.5 text-[10px] text-amber-300 transition hover:bg-amber-400/20 disabled:opacity-40"
          >
            {rolling ? '回滚中…' : '回滚到此版本'}
          </button>
        )}
      </div>
      {version.reason !== '' && <p className="mt-1 truncate text-[11px] text-zinc-500">{version.reason}</p>}
    </li>
  )
}

export default function StrategyVersions({ onRolledBack }: { onRolledBack?: () => void }) {
  const query = useApiData(() => api.getStrategyVersions(), [])
  const [selected, setSelected] = useState<number[]>([]) // diff 选择（至多两个版本 id）
  const [diff, setDiff] = useState<string | null>(null)
  const [diffLoading, setDiffLoading] = useState(false)
  const [diffError, setDiffError] = useState<string | null>(null)
  const [rollingId, setRollingId] = useState<number | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)

  const items = query.data ?? EMPTY_VERSIONS
  // 是否当前：md5 与最新版本（首项）一致的首项。版本表恒最新在前且 items[0].md5 === currentMd5，
  // findIndex 恒为 0——直接取首项（回滚同文产生重复 md5 时旧版本自然不再标当前）
  const currentIndex = items.length === 0 ? -1 : 0

  /** 点选切换：已选则取消；未满两个则追加；已满则挤掉最早选择，保留最近两次点选。 */
  const toggleSelect = (id: number) => {
    setSelected((current) => {
      if (current.includes(id)) return current.filter((x) => x !== id)
      if (current.length < 2) return [...current, id]
      return [current[1], id]
    })
  }

  // 选满两个版本后拉取 diff（固定 旧 → 新）；不足两个时清空旧 diff
  useEffect(() => {
    if (selected.length !== 2) {
      setDiff(null)
      setDiffError(null)
      return
    }
    const from = Math.min(selected[0], selected[1])
    const to = Math.max(selected[0], selected[1])
    let alive = true
    setDiff(null) // 拉取期间清空旧 diff，避免旧版本对的差异行被误读为当前对
    setDiffLoading(true)
    setDiffError(null)
    api
      .getStrategyDiff(from, to)
      .then((text) => {
        if (alive) setDiff(text)
      })
      .catch((e: unknown) => {
        if (alive) setDiffError(e instanceof Error ? e.message : String(e))
      })
      .finally(() => {
        if (alive) setDiffLoading(false)
      })
    return () => {
      alive = false
    }
  }, [selected])

  /** 回滚：确认后调接口，成功刷新版本列表并通知宿主刷新策略编辑器内容。 */
  const rollback = async (id: number) => {
    const confirmed = window.confirm(
      `确认回滚到 v${id}？\n当前策略将被替换为该版本内容（回滚会生成新版本留痕，可再次回滚恢复）。`,
    )
    if (!confirmed) return
    setRollingId(id)
    setNotice(null)
    setActionError(null)
    try {
      const result = await api.rollbackStrategy(id)
      setNotice(`已回滚到 v${result.rolledBackTo}（生成新版本 v${result.version}）`)
      setSelected([]) // 版本表已变化，清空选择避免悬挂 id 指向错位
      query.reload()
      onRolledBack?.()
    } catch (e) {
      setActionError(e instanceof Error ? e.message : String(e))
    } finally {
      setRollingId(null)
    }
  }

  const diffPair = selected.length === 2 ? [Math.min(selected[0], selected[1]), Math.max(selected[0], selected[1])] : null

  return (
    <div className="mt-4">
      <h4 className="mb-2 text-xs tracking-widest text-zinc-500">版本历史 · 点选两个版本对比 diff</h4>

      {notice && (
        <div
          role="status"
          className="mb-2 rounded-lg border border-emerald-400/40 bg-emerald-400/10 px-3 py-2 text-xs text-emerald-300"
        >
          {notice}
        </div>
      )}
      {actionError && (
        <div
          role="alert"
          className="mb-2 rounded-lg border border-rose-500/40 bg-rose-500/10 px-3 py-2 text-xs text-rose-300"
        >
          回滚失败：{actionError}
        </div>
      )}

      <StateHint loading={query.loading} error={query.error} empty={items.length === 0}>
        <ul className="space-y-2">
          {items.map((version, index) => (
            <VersionRow
              key={version.id}
              version={version}
              isCurrent={index === currentIndex}
              selected={selected.includes(version.id)}
              rolling={rollingId === version.id}
              onToggle={() => toggleSelect(version.id)}
              onRollback={() => void rollback(version.id)}
            />
          ))}
        </ul>

        {diffPair && (
          <div className="mt-3">
            <p className="mb-1 text-[11px] text-zinc-500">
              diff v{diffPair[0]} → v{diffPair[1]}
            </p>
            {diffLoading && <p className="py-2 text-xs text-zinc-500">diff 加载中…</p>}
            {diffError && <p className="py-2 text-xs text-rose-400">diff 加载失败：{diffError}</p>}
            {diff !== null &&
              (diff === '' ? (
                // 后端 difflib 对同文版本返回空串：给明确提示而非渲染空白
                <p className="py-2 text-xs text-zinc-500">两版本内容一致</p>
              ) : (
                <pre className="max-h-72 overflow-auto rounded-lg border border-zinc-800 bg-zinc-950 p-3 font-mono text-[11px] leading-5">
                  {diff.split('\n').map((line, i) => (
                    // 空行渲染为空格保持行高；key 用行号（diff 文本无稳定 id）
                    <div key={i} className={diffLineClass(line)}>
                      {line === '' ? ' ' : line}
                    </div>
                  ))}
                </pre>
              ))}
          </div>
        )}
      </StateHint>
    </div>
  )
}
