/**
 * 策略面板（主页面左栏只读视图）测试：
 * 默认展示当前策略全文（GET /api/strategy）；下拉切换历史版本（GET /api/strategy/versions/{id}，
 * 含「历史版本」标识）；refreshKey 变化重拉并复位「当前版本」；加载失败局部降级；
 * 只读不变量——任何交互都不触发 putStrategy/rollbackStrategy。
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { StrategyVersion, StrategyVersionDetail } from '../api/types'
import StrategyPanel from '../components/console/StrategyPanel'

/** 当前策略全文夹具 */
const CURRENT = '# 系统提示词\n\n保本优先，单笔风险不超过权益的 2%。'

/** 版本夹具（最新在前） */
const VERSIONS: StrategyVersion[] = [
  { id: 3, md5: 'md5-c', createdBy: 'review_agent', reason: '复盘：收紧止损', reportId: 2, time: '2026-07-27T03:00:00.000Z' },
  { id: 2, md5: 'md5-b', createdBy: 'human', reason: '', reportId: null, time: '2026-07-26T03:00:00.000Z' },
  { id: 1, md5: 'md5-a', createdBy: 'human', reason: '初始版本', reportId: null, time: '2026-07-25T03:00:00.000Z' },
]

const holder = vi.hoisted(() => ({
  getStrategy: vi.fn(),
  getStrategyVersions: vi.fn(),
  getStrategyVersion: vi.fn(),
  putStrategy: vi.fn(),
  rollbackStrategy: vi.fn(),
}))
vi.mock('../api', () => ({
  api: {
    getStrategy: () => holder.getStrategy(),
    getStrategyVersions: () => holder.getStrategyVersions(),
    getStrategyVersion: (id: number) => holder.getStrategyVersion(id),
    putStrategy: (content: string) => holder.putStrategy(content),
    rollbackStrategy: (id: number) => holder.rollbackStrategy(id),
  },
}))

beforeEach(() => {
  vi.clearAllMocks()
  holder.getStrategy.mockImplementation(() => Promise.resolve(CURRENT))
  holder.getStrategyVersions.mockImplementation(() => Promise.resolve([...VERSIONS]))
  holder.getStrategyVersion.mockImplementation((id: number) => {
    const version = VERSIONS.find((v) => v.id === id)
    if (!version) return Promise.reject(new Error(`策略版本不存在: ${id}`))
    return Promise.resolve({ ...version, content: `# v${version.id} 旧策略\n\n历史版本全文。` })
  })
})

/** 版本下拉（带显式类型，便于断言 value） */
function versionSelect(): HTMLSelectElement {
  return screen.getByRole('combobox', { name: '选择策略版本' }) as HTMLSelectElement
}

describe('StrategyPanel(策略面板)', () => {
  it('默认视图：渲染当前策略全文 + 版本下拉（当前版本 + 各历史版本）', async () => {
    render(<StrategyPanel refreshKey={0} onOpenConfig={() => {}} />)

    expect(await screen.findByText(/保本优先/)).toBeInTheDocument()
    expect(versionSelect().value).toBe('')
    expect(screen.getByRole('option', { name: '当前版本' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: /v3 · 复盘/ })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: /v1 · 人工/ })).toBeInTheDocument()
    // 默认不拉取任何版本详情
    expect(holder.getStrategyVersion).not.toHaveBeenCalled()
  })

  it('切换历史版本：按 id 拉取全文并显示徽标/历史版本标识；切回「当前版本」恢复当前文本', async () => {
    render(<StrategyPanel refreshKey={0} onOpenConfig={() => {}} />)
    await screen.findByText(/保本优先/)

    fireEvent.change(versionSelect(), { target: { value: '2' } })

    await waitFor(() => expect(holder.getStrategyVersion).toHaveBeenCalledWith(2))
    expect(await screen.findByText(/历史版本全文/)).toBeInTheDocument()
    expect(screen.getByText('历史版本')).toBeInTheDocument()
    expect(screen.getByText('v2')).toBeInTheDocument()
    // 历史视图下当前策略文本被替换
    expect(screen.queryByText(/保本优先/)).not.toBeInTheDocument()

    fireEvent.change(versionSelect(), { target: { value: '' } })

    expect(await screen.findByText(/保本优先/)).toBeInTheDocument()
    expect(screen.queryByText('历史版本')).not.toBeInTheDocument()
    // 只读不变量：全程不触发任何写接口
    expect(holder.putStrategy).not.toHaveBeenCalled()
    expect(holder.rollbackStrategy).not.toHaveBeenCalled()
  })

  it('refreshKey 变化：重拉当前策略与版本表，且视图复位到「当前版本」', async () => {
    const { rerender } = render(<StrategyPanel refreshKey={0} onOpenConfig={() => {}} />)
    await screen.findByText(/保本优先/)
    expect(holder.getStrategy).toHaveBeenCalledTimes(1)
    expect(holder.getStrategyVersions).toHaveBeenCalledTimes(1)

    // 先选中历史版本，再 bump refreshKey（模拟抽屉内保存/回滚后关闭抽屉）
    fireEvent.change(versionSelect(), { target: { value: '2' } })
    await screen.findByText(/历史版本全文/)
    rerender(<StrategyPanel refreshKey={1} onOpenConfig={() => {}} />)

    await waitFor(() => expect(holder.getStrategy).toHaveBeenCalledTimes(2))
    expect(holder.getStrategyVersions).toHaveBeenCalledTimes(2)
    // 复位：回到当前策略文本，下拉值清空，不再显示历史标识
    expect(await screen.findByText(/保本优先/)).toBeInTheDocument()
    expect(versionSelect().value).toBe('')
    expect(screen.queryByText('历史版本')).not.toBeInTheDocument()
  })

  it('当前策略加载失败：局部错误提示，版本下拉仍可用（不影响面板其他区域）', async () => {
    holder.getStrategy.mockRejectedValue(new Error('boom'))
    render(<StrategyPanel refreshKey={0} onOpenConfig={() => {}} />)

    expect(await screen.findByText(/加载失败：boom/)).toBeInTheDocument()
    expect(await screen.findByRole('combobox', { name: '选择策略版本' })).toBeInTheDocument()
  })

  it('「去配置中心修改」按钮回调 onOpenConfig', async () => {
    const onOpenConfig = vi.fn()
    render(<StrategyPanel refreshKey={0} onOpenConfig={onOpenConfig} />)

    fireEvent.click(screen.getByRole('button', { name: '去配置中心修改' }))
    expect(onOpenConfig).toHaveBeenCalledTimes(1)
  })

  it('切换版本的加载窗口内不残留上一版本的全文与徽标', async () => {
    // v3 详情请求挂起（deferred），复现慢后端加载窗口；初始空操作保证 TS 不把变量窄化为 null
    let resolveV3: (value: StrategyVersionDetail) => void = () => {}
    holder.getStrategyVersion.mockImplementation((id: number) => {
      if (id === 3)
        return new Promise<StrategyVersionDetail>((resolve) => {
          resolveV3 = resolve
        })
      const version = VERSIONS.find((v) => v.id === id)
      if (!version) return Promise.reject(new Error(`策略版本不存在: ${id}`))
      return Promise.resolve({ ...version, content: `# v${version.id} 旧策略\n\n历史版本全文。` })
    })
    render(<StrategyPanel refreshKey={0} onOpenConfig={() => {}} />)
    await screen.findByText(/保本优先/)

    // 先选中 v2 并等其全文显示
    fireEvent.change(versionSelect(), { target: { value: '2' } })
    expect(await screen.findByText(/历史版本全文/)).toBeInTheDocument()
    expect(screen.getByText('v2')).toBeInTheDocument()

    // 切到 v3：请求挂起期间，v2 的全文与徽标必须消失（下拉值已是 3，内容不得错配）
    fireEvent.change(versionSelect(), { target: { value: '3' } })
    expect(await screen.findByText('版本内容加载中…')).toBeInTheDocument()
    expect(screen.queryByText(/历史版本全文/)).not.toBeInTheDocument()
    expect(screen.queryByText('v2')).not.toBeInTheDocument()

    // v3 到位后正常显示新内容
    resolveV3({ ...VERSIONS[0], content: '# v3 新策略全文' })
    expect(await screen.findByText(/v3 新策略全文/)).toBeInTheDocument()
  })

  it('版本详情加载失败：错误提示不与上一版本全文并列', async () => {
    holder.getStrategyVersion.mockImplementation((id: number) => {
      if (id === 3) return Promise.reject(new Error('net-fail'))
      const version = VERSIONS.find((v) => v.id === id)
      if (!version) return Promise.reject(new Error(`策略版本不存在: ${id}`))
      return Promise.resolve({ ...version, content: `# v${version.id} 旧策略\n\n历史版本全文。` })
    })
    render(<StrategyPanel refreshKey={0} onOpenConfig={() => {}} />)
    await screen.findByText(/保本优先/)

    fireEvent.change(versionSelect(), { target: { value: '2' } })
    await screen.findByText(/历史版本全文/)

    fireEvent.change(versionSelect(), { target: { value: '3' } })
    expect(await screen.findByText(/加载失败：net-fail/)).toBeInTheDocument()
    // 失败终态：v2 的全文与徽标不得残留（用户没有任何机制辨别正文不是 v3）
    expect(screen.queryByText(/历史版本全文/)).not.toBeInTheDocument()
    expect(screen.queryByText('v2')).not.toBeInTheDocument()
  })

  it('refreshKey 重拉期间保留当前策略全文，后台刷新不闪烁「加载中…」', async () => {
    // 第二次 getStrategy 挂起，复现 WS 决策轮事件触发的后台重拉窗口
    holder.getStrategy
      .mockImplementationOnce(() => Promise.resolve(CURRENT))
      .mockImplementationOnce(() => new Promise<string>(() => {}))
    const { rerender } = render(<StrategyPanel refreshKey={0} onOpenConfig={() => {}} />)
    await screen.findByText(/保本优先/)

    rerender(<StrategyPanel refreshKey={1} onOpenConfig={() => {}} />)

    // 重拉已发起，但旧全文必须保留在屏幕上
    await waitFor(() => expect(holder.getStrategy).toHaveBeenCalledTimes(2))
    expect(screen.getByText(/保本优先/)).toBeInTheDocument()
    expect(screen.queryByText('加载中…')).not.toBeInTheDocument()
  })

  it('版本列表加载失败：非阻断提示，当前策略全文仍正常展示（局部降级）', async () => {
    holder.getStrategyVersions.mockRejectedValue(new Error('ver-boom'))
    render(<StrategyPanel refreshKey={0} onOpenConfig={() => {}} />)

    expect(await screen.findByText('版本列表加载失败')).toBeInTheDocument()
    expect(await screen.findByText(/保本优先/)).toBeInTheDocument()
    expect(screen.queryByRole('combobox', { name: '选择策略版本' })).not.toBeInTheDocument()
  })

  it('版本列表为空：隐藏版本下拉，当前策略全文正常展示', async () => {
    holder.getStrategyVersions.mockResolvedValue([])
    render(<StrategyPanel refreshKey={0} onOpenConfig={() => {}} />)

    expect(await screen.findByText(/保本优先/)).toBeInTheDocument()
    expect(screen.queryByRole('combobox', { name: '选择策略版本' })).not.toBeInTheDocument()
    expect(screen.queryByText('版本列表加载失败')).not.toBeInTheDocument()
  })
})
