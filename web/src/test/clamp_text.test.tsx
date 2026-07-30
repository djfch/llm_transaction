/** ClampText 行数折叠测试：溢出显示「…展开」、点击展开/收起、不溢出无按钮。 */
import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import ClampText from '../components/console/ClampText'

/**
 * jsdom 无布局，scrollHeight/clientHeight 恒为 0（视为不溢出）。
 * 通过原型级 getter 桩造出「内容高于容器」的溢出场景。
 */
function mockElementHeights(scrollHeight: number, clientHeight: number) {
  Object.defineProperty(HTMLElement.prototype, 'scrollHeight', {
    configurable: true,
    get: () => scrollHeight,
  })
  Object.defineProperty(HTMLElement.prototype, 'clientHeight', {
    configurable: true,
    get: () => clientHeight,
  })
}

afterEach(() => {
  // 还原原型，避免污染其他用例
  delete (HTMLElement.prototype as { scrollHeight?: number }).scrollHeight
  delete (HTMLElement.prototype as { clientHeight?: number }).clientHeight
})

describe('ClampText(行数折叠)', () => {
  it('内容溢出时显示「…展开」，点击后展开全文并可收起', () => {
    mockElementHeights(240, 120)
    render(<ClampText text="超长内容" clampClass="line-clamp-5" />)

    const content = screen.getByText('超长内容')
    expect(content.className).toContain('line-clamp-5')

    const toggle = screen.getByRole('button', { name: '…展开' })
    fireEvent.click(toggle)
    expect(content.className).not.toContain('line-clamp-5')
    expect(screen.getByRole('button', { name: '收起' })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '收起' }))
    expect(content.className).toContain('line-clamp-5')
    expect(screen.getByRole('button', { name: '…展开' })).toBeInTheDocument()
  })

  it('内容未溢出时不渲染展开按钮', () => {
    mockElementHeights(120, 120)
    render(<ClampText text="短内容" clampClass="line-clamp-5" />)

    expect(screen.getByText('短内容')).toBeInTheDocument()
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
  })

  it('展开按钮点击不向外冒泡（不误触卡片手风琴）', () => {
    mockElementHeights(240, 120)
    let outerClicks = 0
    render(
      <div onClick={() => (outerClicks += 1)}>
        <ClampText text="超长内容" clampClass="line-clamp-5" />
      </div>,
    )

    fireEvent.click(screen.getByRole('button', { name: '…展开' }))
    expect(outerClicks).toBe(0)
  })
})
