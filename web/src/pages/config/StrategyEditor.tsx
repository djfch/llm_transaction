/**
 * system_prompt.md 在线编辑：等宽 textarea + 保存（方案 C 抽屉样式）。
 */
import { useState } from 'react'

export default function StrategyEditor({
  initial,
  onSave,
}: {
  initial: string
  onSave: (content: string) => Promise<void>
}) {
  const [content, setContent] = useState(initial)
  const [pending, setPending] = useState(false)
  const [savedAt, setSavedAt] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const dirty = content !== initial

  const handleSave = async () => {
    setPending(true)
    setError(null)
    try {
      await onSave(content)
      setSavedAt(new Date().toLocaleTimeString('zh-CN', { hour12: false }))
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setPending(false)
    }
  }

  return (
    <div>
      <textarea
        value={content}
        onChange={(e) => {
          setContent(e.target.value)
          setSavedAt(null)
        }}
        rows={14}
        spellCheck={false}
        aria-label="system_prompt 内容"
        className="w-full rounded-lg border border-white/10 bg-zinc-950 p-4 font-mono text-xs leading-relaxed text-zinc-200 focus:border-violet-400/60 focus:outline-none"
      />
      <div className="mt-3 flex items-center gap-3">
        <button
          type="button"
          disabled={pending || !dirty}
          onClick={handleSave}
          className="rounded-lg border border-violet-400/50 bg-violet-400/10 px-3 py-1.5 text-xs text-violet-300 transition hover:bg-violet-400/20 disabled:opacity-40"
        >
          {pending ? '保存中…' : '保存并热更新'}
        </button>
        {dirty && <span className="text-xs text-amber-400">有未保存修改</span>}
        {savedAt && !dirty && <span className="text-xs text-emerald-400">已保存 {savedAt}</span>}
        {error && <span className="text-xs text-rose-400">保存失败：{error}</span>}
        <span className="ml-auto font-mono text-[10px] text-zinc-600">{content.length} 字符</span>
      </div>
    </div>
  )
}
