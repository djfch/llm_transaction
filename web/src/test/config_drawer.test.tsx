/**
 * 配置抽屉集成测试：回滚成功后提示可见、策略编辑器内容同步为目标版本。
 * 不变量：抽屉内表单数据已就绪时，后台刷新不得销毁用户可见状态（提示/已保存标记/未保存编辑）。
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { AppConfig, AppConfigPatch, StrategyVersion } from '../api/types'
import ConfigDrawer from '../components/console/ConfigDrawer'

/** 配置夹具（paper 模式，触发权益重置小节；与 console_page.test.tsx 同构） */
const CONFIG: AppConfig = {
  mode: 'paper',
  llm: {
    provider: 'anthropic',
    model: 'claude-sonnet-4-5',
    max_tokens: 4096,
    openai_base_url: '',
    thinking_effort: '',
    max_consecutive_failures: 3,
  },
  risk: {
    max_position_pct: 0.3,
    max_total_position_pct: 0.8,
    max_leverage: 5,
    daily_loss_limit: 0.1,
    max_orders_per_day: 20,
    max_deviation: 0.02,
    kill_switch: false,
  },
  scheduler: { default_wake_minutes: 60, min_wake_minutes: 5, max_wake_minutes: 720 },
  notify: { telegram_enabled: false },
}

/** 版本夹具（最新在前）：v2 为当前（唯一非当前 v1 才有回滚按钮） */
const VERSIONS: StrategyVersion[] = [
  { id: 2, md5: 'md5-b', createdBy: 'review_agent', reason: '复盘改写', reportId: 1, time: '2026-07-26T03:00:00.000Z' },
  { id: 1, md5: 'md5-a', createdBy: 'human', reason: '初始版本', reportId: null, time: '2026-07-25T03:00:00.000Z' },
]

const holder = vi.hoisted(() => ({
  getStrategy: vi.fn(),
  getStrategyVersions: vi.fn(),
  getStrategyDiff: vi.fn(),
  rollbackStrategy: vi.fn(),
  putStrategy: vi.fn(),
  putConfig: vi.fn(),
}))

vi.mock('../api', () => ({
  api: {
    getConfig: () => Promise.resolve(CONFIG),
    putConfig: (body: AppConfigPatch) => holder.putConfig(body),
    getResearchScheduleStatus: () => Promise.resolve({
      enabled: false,
      items: [],
      calendar: { state: 'ok', last_refreshed_at: null, errors: {}, warning: '' },
    }),
    getSecretsStatus: () => Promise.resolve({ gate_key: true, llm_key: true, telegram: false }),
    getWatchlist: () => Promise.resolve({ settle: 'usdt', contracts: ['BTC_USDT'] }),
    getStrategy: () => holder.getStrategy(),
    getStrategyVersions: () => holder.getStrategyVersions(),
    getStrategyDiff: (from: number, to: number) => holder.getStrategyDiff(from, to),
    rollbackStrategy: (id: number) => holder.rollbackStrategy(id),
    putStrategy: (content: string) => holder.putStrategy(content),
  },
  // StrategyVersions catch 分支仅取 message，但保持与生产 mock 一致的透出形态
  ApiError: class ApiError extends Error {},
}))

let strategyText: string
let versions: StrategyVersion[]

beforeEach(() => {
  strategyText = '策略书 v2 全文'
  versions = [...VERSIONS]
  vi.clearAllMocks()
  holder.getStrategy.mockImplementation(() => Promise.resolve(strategyText))
  holder.getStrategyVersions.mockImplementation(() => Promise.resolve([...versions]))
  holder.getStrategyDiff.mockImplementation(() => Promise.resolve(''))
  holder.rollbackStrategy.mockImplementation((id: number) => {
    if (id === 1) strategyText = '策略书 v1 全文'
    versions = [
      { id: 3, md5: 'md5-a', createdBy: 'rollback', reason: `回滚到 v${id}`, reportId: null, time: '2026-07-27T04:00:00.000Z' },
      ...versions,
    ]
    return Promise.resolve({ rolledBackTo: id, version: 3 })
  })
  // 保存策略：更新全文并生成新版本（与后端 StrategyStore 版本落库行为一致）
  holder.putStrategy.mockImplementation((content: string) => {
    strategyText = content
    versions = [
      { id: 3, md5: 'md5-c', createdBy: 'human', reason: '手动保存', reportId: null, time: '2026-07-28T04:00:00.000Z' },
      ...versions,
    ]
    return Promise.resolve(content)
  })
  holder.putConfig.mockResolvedValue({ saved: true, needs_restart: [] })
})

afterEach(() => vi.unstubAllGlobals())

describe('ConfigDrawer(配置抽屉) · 策略版本回滚', () => {
  it('回滚成功：提示在 strategyQ 刷新期间不随 DrawerSection 卸载而消失', async () => {
    vi.stubGlobal('confirm', vi.fn().mockReturnValue(true))
    render(<ConfigDrawer open onClose={() => {}} />)

    // 等策略小节就绪：编辑器初值 + 版本列表渲染
    expect(await screen.findByLabelText('system_prompt 内容')).toHaveValue('策略书 v2 全文')
    await screen.findByText('初始版本')
    const strategyCallsBefore = holder.getStrategy.mock.calls.length

    // 回滚触发的 strategyQ.reload 挂起：让「后台刷新中」窗口可观察
    let resolveStrategy: ((value: string) => void) | null = null
    holder.getStrategy.mockImplementationOnce(
      () =>
        new Promise<string>((resolve) => {
          resolveStrategy = resolve
        }),
    )

    // v1 是唯一非当前版本，只有一枚回滚按钮
    fireEvent.click(screen.getByRole('button', { name: '回滚到此版本' }))

    await waitFor(() => expect(holder.rollbackStrategy).toHaveBeenCalledWith(1))
    await waitFor(() => expect(holder.getStrategy).toHaveBeenCalledTimes(strategyCallsBefore + 1))

    // strategyQ 刷新尚未完成时，成功提示必须仍然可见
    expect(screen.getByText('已回滚到 v1（生成新版本 v3）')).toBeInTheDocument()

    // 刷新完成后：编辑器同步为目标版本内容，提示依然在；版本列表 v3（rollback）置顶标当前
    resolveStrategy!('策略书 v1 全文')
    const textarea = await screen.findByLabelText('system_prompt 内容')
    await waitFor(() => expect(textarea).toHaveValue('策略书 v1 全文'))
    expect(screen.getByText('已回滚到 v1（生成新版本 v3）')).toBeInTheDocument()
    expect(await screen.findByText('v3')).toBeInTheDocument()
    expect(screen.getByText('回滚')).toBeInTheDocument()
    expect(screen.getAllByText('当前')).toHaveLength(1)
  })

  it('保存并热更新成功：版本历史立即重拉，新版本行可见', async () => {
    render(<ConfigDrawer open onClose={() => {}} />)

    // 等策略小节就绪：编辑器初值 + 版本列表渲染
    const textarea = await screen.findByLabelText('system_prompt 内容')
    expect(textarea).toHaveValue('策略书 v2 全文')
    await screen.findByText('初始版本')
    const versionCallsBefore = holder.getStrategyVersions.mock.calls.length

    // 修改内容并保存
    fireEvent.change(textarea, { target: { value: '策略书 v3 手改全文' } })
    fireEvent.click(screen.getByRole('button', { name: '保存并热更新' }))

    await waitFor(() => expect(holder.putStrategy).toHaveBeenCalledWith('策略书 v3 手改全文'))
    // 保存后版本列表查询必须再次触发
    await waitFor(() =>
      expect(holder.getStrategyVersions.mock.calls.length).toBeGreaterThan(versionCallsBefore),
    )
    // 新版本行出现并标「当前」，编辑器显示已保存标记
    expect(await screen.findByText('v3')).toBeInTheDocument()
    expect(screen.getByText('手动保存')).toBeInTheDocument()
    expect(screen.getByText(/^已保存 /)).toBeInTheDocument()
  })

  it('研报表单仅提交 research 段，避免旧快照覆盖其他配置', async () => {
    render(<ConfigDrawer open onClose={() => {}} />)

    await screen.findByRole('checkbox', { name: '启用自动研报' })
    fireEvent.click(screen.getByRole('checkbox', { name: '启用自动研报' }))
    fireEvent.click(screen.getByRole('button', { name: '保存研报设置' }))

    await waitFor(() => expect(holder.putConfig).toHaveBeenCalledTimes(1))
    expect(holder.putConfig.mock.calls[0][0]).toEqual({
      research: {
        enabled: true,
        schedules: [
          { id: 'asia_open', kind: 'market_open', market: 'XTKS', enabled: true, lead_minutes: 30 },
          { id: 'europe_open', kind: 'market_open', market: 'XLON', enabled: true, lead_minutes: 30 },
          { id: 'us_open', kind: 'market_open', market: 'XNYS', enabled: true, lead_minutes: 30 },
        ],
      },
    })
    expect(screen.getByText('已生效')).toBeInTheDocument()
  })
})
