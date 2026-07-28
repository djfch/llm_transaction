/**
 * 复盘报告面板测试：列表渲染（时间/复盘区间/动作徽标/error 红字）、展开详情
 * （statsJson 统计表格 + reportMd 全文，字段缺失降级）、「立即复盘」成功刷新
 * 与 409/503 的 ApiError.detail 提示、服务端分页。
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../api/http'
import type { ReviewReport, ReviewReportSummary } from '../api/types'
import ReviewPanel from '../components/console/ReviewPanel'

const iso = (unixSec: number) => new Date(unixSec * 1000).toISOString()

/**
 * 7 条报告覆盖两页（最新在前）：id7 失败红字行；id6 改策略（统计三键齐全）；
 * id5~1 未调整（statsJson 仅 total_pnl，用于降级形态）。
 */
const REPORTS: ReviewReportSummary[] = [
  {
    id: 7,
    periodStart: iso(1784505600),
    periodEnd: iso(1784592000),
    statsJson: '{}',
    reportMd: '',
    strategyAction: 'none',
    newVersionId: null,
    error: 'LLM 响应超时',
    time: iso(1784600000),
  },
  {
    id: 6,
    periodStart: iso(1784419200),
    periodEnd: iso(1784505600),
    statsJson: '{"close_count":5,"total_pnl":"-32.10","win_rate":"0.4000","profit_factor":"0.5847"}',
    reportMd: '# 复盘报告\n\n本区间亏损。',
    strategyAction: 'rewrite',
    newVersionId: 3,
    error: '',
    time: iso(1784510000),
  },
  ...Array.from({ length: 5 }, (_, i) => {
    const id = 5 - i
    return {
      id,
      periodStart: iso(1784332800 - i * 86400),
      periodEnd: iso(1784419200 - i * 86400),
      statsJson: '{"close_count":3,"total_pnl":"18.40"}',
      reportMd: `第 ${id} 份报告：无需调整。`,
      strategyAction: 'none' as const,
      newVersionId: null,
      error: '',
      time: iso(1784420000 - i * 3600),
    }
  }),
]

const holder = vi.hoisted(() => ({
  getReviewReports: vi.fn(),
  getReviewReport: vi.fn(),
  runReview: vi.fn(),
}))
vi.mock('../api', () => ({
  api: {
    getReviewReports: (offset: number, limit: number) => holder.getReviewReports(offset, limit),
    getReviewReport: (id: number) => holder.getReviewReport(id),
    runReview: () => holder.runReview(),
  },
}))

beforeEach(() => {
  vi.clearAllMocks()
  holder.getReviewReports.mockImplementation((offset: number, limit: number) =>
    Promise.resolve({ items: REPORTS.slice(offset, offset + limit), total: REPORTS.length }),
  )
  // 详情在列表基础上追加「完整版追加段落」，验证展开时 lazy 拉取的是全文而非列表截断
  holder.getReviewReport.mockImplementation((id: number): Promise<ReviewReport> => {
    const found = REPORTS.find((r) => r.id === id)
    if (!found) return Promise.reject(new ApiError(404, `复盘报告不存在: ${id}`))
    const fullMd = found.reportMd === '' ? '' : `${found.reportMd}\n\n完整版追加段落。`
    return Promise.resolve({ ...found, reportMd: fullMd })
  })
  holder.runReview.mockImplementation(() =>
    Promise.resolve({
      started: true,
      ok: true,
      reportId: 8,
      roundId: 'rv-8',
      strategyAction: 'none',
      newVersionId: null,
      error: '',
    }),
  )
})

describe('ReviewPanel(复盘报告)', () => {
  it('列表渲染：error 红字行 / 改策略徽标 / 未调整徽标 / 复盘区间 / 分页摘要', async () => {
    render(<ReviewPanel />)

    expect(await screen.findByText('复盘失败：LLM 响应超时')).toBeInTheDocument()
    expect(screen.getByText('改策略 → v3')).toBeInTheDocument()
    expect(screen.getAllByText('未调整').length).toBe(4) // id7/5/4/3 四条 none（失败行同样带动作徽标）
    expect(screen.getAllByText(/区间 .+ ~ .+/).length).toBe(5)
    expect(screen.getByText('第 1/2 页 · 共 7 条复盘')).toBeInTheDocument()
    expect(holder.getReviewReports).toHaveBeenCalledWith(0, 5)
  })

  it('点击展开：lazy 拉取全文，展示统计表格与 reportMd 完整内容', async () => {
    render(<ReviewPanel />)
    const preview = await screen.findByText(/本区间亏损。/)

    fireEvent.click(preview)
    await waitFor(() => expect(holder.getReviewReport).toHaveBeenCalledWith(6))

    // statsJson → 总盈亏/胜率/盈亏比 三行（负盈亏红色口径由 class 保证，此处断文本）
    expect(await screen.findByText('总盈亏')).toBeInTheDocument()
    expect(screen.getByText('-32.10')).toBeInTheDocument()
    expect(screen.getByText('胜率')).toBeInTheDocument()
    expect(screen.getByText('40.00%')).toBeInTheDocument()
    expect(screen.getByText('盈亏比')).toBeInTheDocument()
    expect(screen.getByText('0.58')).toBeInTheDocument()
    // 全文（列表预览不含追加段落）
    expect(await screen.findByText(/完整版追加段落。/)).toBeInTheDocument()
  })

  it('statsJson 为空对象时降级：展开不渲染统计表格行', async () => {
    render(<ReviewPanel />)
    const errorRow = await screen.findByText('复盘失败：LLM 响应超时')

    fireEvent.click(errorRow)
    await waitFor(() => expect(holder.getReviewReport).toHaveBeenCalledWith(7))
    await waitFor(() => expect(screen.queryByText('报告全文加载中…')).not.toBeInTheDocument())

    expect(screen.queryByText('总盈亏')).not.toBeInTheDocument()
    expect(screen.queryByText('胜率')).not.toBeInTheDocument()
    expect(screen.queryByText('盈亏比')).not.toBeInTheDocument()
  })

  it('立即复盘成功：提示成功并刷新列表', async () => {
    render(<ReviewPanel />)
    await screen.findByText(/第 5 份报告/)
    const callsBefore = holder.getReviewReports.mock.calls.length

    fireEvent.click(screen.getByRole('button', { name: '立即复盘' }))

    expect(await screen.findByText('复盘已完成，最新报告已入列')).toBeInTheDocument()
    expect(holder.runReview).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(holder.getReviewReports).toHaveBeenCalledTimes(callsBefore + 1))
  })

  it('立即复盘 409：展示 ApiError.detail（复盘进行中）', async () => {
    holder.runReview.mockRejectedValueOnce(new ApiError(409, '复盘进行中'))
    render(<ReviewPanel />)
    await screen.findByText(/第 5 份报告/)

    fireEvent.click(screen.getByRole('button', { name: '立即复盘' }))

    expect(await screen.findByText('复盘进行中')).toBeInTheDocument()
  })

  it('立即复盘 503：展示 ApiError.detail（LLM 未配置）', async () => {
    holder.runReview.mockRejectedValueOnce(new ApiError(503, 'LLM 未配置'))
    render(<ReviewPanel />)
    await screen.findByText(/第 5 份报告/)

    fireEvent.click(screen.getByRole('button', { name: '立即复盘' }))

    expect(await screen.findByText('LLM 未配置')).toBeInTheDocument()
  })

  it('立即复盘返回 started=true 但 ok=false（复盘执行失败）：红色提示失败原因且仍刷新列表', async () => {
    // 后端路由仅把「LLM 未配置」「复盘进行中」映 503/409，其余失败以 200 返回 ok=false（失败报告已落库）
    holder.runReview.mockResolvedValueOnce({
      started: true,
      ok: false,
      reportId: 8,
      roundId: 'rv-8',
      strategyAction: 'none',
      newVersionId: null,
      error: 'LLM 调用异常',
    })
    render(<ReviewPanel />)
    await screen.findByText(/第 5 份报告/)
    const callsBefore = holder.getReviewReports.mock.calls.length

    fireEvent.click(screen.getByRole('button', { name: '立即复盘' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('复盘失败：LLM 调用异常')
    await waitFor(() => expect(holder.getReviewReports).toHaveBeenCalledTimes(callsBefore + 1))
  })

  it('分页：下一页拉取 offset=5 并渲染第二页内容', async () => {
    render(<ReviewPanel />)
    await screen.findByText(/第 5 份报告/)

    fireEvent.click(screen.getByRole('button', { name: '下一页' }))

    await waitFor(() => expect(holder.getReviewReports).toHaveBeenLastCalledWith(5, 5))
    expect(await screen.findByText('第 2 份报告：无需调整。')).toBeInTheDocument()
    expect(screen.getByText('第 1 份报告：无需调整。')).toBeInTheDocument()
    expect(screen.getByText('第 2/2 页 · 共 7 条复盘')).toBeInTheDocument()
  })
})
