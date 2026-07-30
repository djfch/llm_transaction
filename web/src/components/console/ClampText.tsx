/**
 * 行数折叠文本：默认按 clampClass（line-clamp-N 静态类名）截断，
 * 溢出时显示「…展开 / 收起」文字按钮；不溢出则不出现按钮，与直接渲染等价。
 * 用于决策时间线摘要、笔记引文与 Agent 笔记卡片，避免长文本撑爆面板。
 */
import { useLayoutEffect, useRef, useState } from 'react'
import type { MouseEvent } from 'react'

interface ClampTextProps {
  text: string
  clampClass: string // 必须传静态类名（如 line-clamp-5），Tailwind JIT 不识别动态拼接
  className?: string // 内容段落的排版样式（字号/行高/颜色等）
}

export default function ClampText({ text, clampClass, className = '' }: ClampTextProps) {
  const [open, setOpen] = useState(false)
  const [overflow, setOverflow] = useState(false)
  const ref = useRef<HTMLParagraphElement>(null)

  // 收起态测量是否溢出（scrollHeight > clientHeight）；text 变化时重测。
  // 展开态跳过测量（此时无截断），保留上次结果以继续显示「收起」按钮。
  useLayoutEffect(() => {
    if (open) return
    const el = ref.current
    if (el) setOverflow(el.scrollHeight > el.clientHeight)
  }, [text, open])

  // 阻止冒泡：卡片外层可能是手风琴按钮，展开全文不应触发卡片展开
  const toggle = (e: MouseEvent<HTMLButtonElement>) => {
    e.stopPropagation()
    setOpen((o) => !o)
  }

  return (
    <div>
      <p ref={ref} className={`break-words ${open ? '' : clampClass} ${className}`}>
        {text}
      </p>
      {overflow && (
        <button
          type="button"
          onClick={toggle}
          className="mt-1 text-xs text-violet-300 transition hover:text-violet-200"
        >
          {open ? '收起' : '…展开'}
        </button>
      )}
    </div>
  )
}
