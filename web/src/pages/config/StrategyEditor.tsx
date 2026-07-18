/**
 * system_prompt.md 在线编辑：等宽 textarea + 保存。
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
        rows={16}
        spellCheck={false}
        aria-label="system_prompt 内容"
        className="w-full rounded-lg border border-slate-700 bg-slate-950 p-4 font-mono text-xs leading-relaxed text-slate-200 focus:border-sky-500 focus:outline-none"
      />
      <div className="mt-3 flex items-center gap-3">
        <button
          type="button"
          disabled={pending || !dirty}
          onClick={handleSave}
          className="rounded-lg bg-sky-600 px-4 py-2 text-sm font-medium text-white hover:bg-sky-500 disabled:opacity-40"
        >
          {pending ? '保存中…' : '保存 system_prompt.md'}
        </button>
        {dirty && <span className="text-xs text-amber-400">有未保存修改</span>}
        {savedAt && !dirty && <span className="text-xs text-emerald-400">已保存 {savedAt}</span>}
        {error && <span className="text-xs text-rose-400">保存失败：{error}</span>}
        <span className="ml-auto text-xs text-slate-500">{content.length} 字符</span>
      </div>
    </div>
  )
}
