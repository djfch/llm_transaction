/**
 * AI 大脑观察舱顶部状态栏：
 * 左：产品名 + mode(运行模式) 徽标；中：Agent 启停 + kill_switch；右：LLM 状态 / WS 指示灯 / 配置入口。
 * llm_configured=false 时在栏下渲染琥珀色横幅（自动决策暂停）。
 * 数据由父级装配层下发（哑组件）；启停/kill 写操作沿用 ApiError 错误展示（由复用按钮内部处理）。
 */
import { api } from '../../api'
import type { StatusInfo } from '../../api/types'
import AgentControl from '../AgentControl'
import KillSwitchButton from '../KillSwitchButton'
import { fmtUptime } from '../../utils/format'

/** mode(运行模式) → 徽标文案与配色：paper 琥珀 / testnet 青 / live 红 */
const MODE_BADGE: Record<string, { text: string; cls: string }> = {
  paper: { text: 'PAPER · 模拟盘', cls: 'border-amber-300/40 bg-amber-300/10 text-amber-300' },
  testnet: { text: 'TESTNET · 沙盒', cls: 'border-cyan-300/40 bg-cyan-400/10 text-cyan-300' },
  live: { text: 'LIVE · 实盘', cls: 'border-rose-500/50 bg-rose-500/10 text-rose-400' },
}

/** 品牌区：神经元图标 + 产品名 */
function Brand() {
  return (
    <div className="flex items-center gap-2.5 pr-1">
      <div className="flex h-8 w-8 items-center justify-center rounded-lg border border-violet-400/40 bg-gradient-to-br from-violet-500/30 to-cyan-400/20">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#a78bfa" strokeWidth="1.8">
          <circle cx="12" cy="12" r="3" />
          <circle cx="4" cy="6" r="2" />
          <circle cx="20" cy="6" r="2" />
          <circle cx="4" cy="18" r="2" />
          <circle cx="20" cy="18" r="2" />
          <path d="M5.5 7.4 9.8 10.3M18.5 7.4l-4.3 2.9M5.5 16.6l4.3-2.9M18.5 16.6l-4.3-2.9" />
        </svg>
      </div>
      <div className="leading-tight">
        <div className="font-bold tracking-wide text-zinc-100">
          LLM 交易 <span className="text-violet-300">Agent</span>
        </div>
        <div className="text-[10px] text-zinc-500">Gate.io 永续 · AI 大脑观察舱</div>
      </div>
    </div>
  )
}

/** mode(运行模式) 徽标；status 未加载时显示占位 */
function ModeBadge({ mode }: { mode: string | undefined }) {
  const badge = mode ? MODE_BADGE[mode] : undefined
  const cls = badge?.cls ?? 'border-white/10 bg-white/5 text-zinc-500'
  return (
    <span
      className={`rounded-md border px-2.5 py-1 font-mono text-xs font-semibold tracking-widest ${cls}`}
    >
      {badge?.text ?? '…'}
    </span>
  )
}

/** LLM 状态：决策凭证 provider/model（name / thinking_effort）+ 配置徽标（未配置琥珀） */
function LlmStatus({ status }: { status: StatusInfo | null }) {
  if (!status) return <span className="text-xs text-zinc-600">LLM …</span>
  const ok = status.llm_configured
  const thinkingEffort = status.llm_thinking_effort || '模型默认'
  return (
    <div className="hidden items-center gap-1.5 text-xs text-zinc-400 xl:flex">
      <span className={`h-1.5 w-1.5 rounded-full ${ok ? 'bg-emerald-400' : 'bg-amber-400'}`} />
      LLM{' '}
      <span className="font-mono text-zinc-200">
        {`${status.llm_provider} · ${status.llm_model}（${status.llm_credential_name} / ${thinkingEffort}）`}
      </span>
      <span className={ok ? 'text-emerald-400/90' : 'font-semibold text-amber-300'}>
        {ok ? '已配置' : '未配置'}
      </span>
    </div>
  )
}

/** WS 连接指示灯：已连接绿（带 ping 动画）/ 断开灰 */
function WsDot({ connected }: { connected: boolean }) {
  return (
    <div className="hidden items-center gap-1.5 text-xs text-zinc-400 md:flex">
      <span className="relative flex h-2 w-2">
        {connected && (
          <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-400 opacity-60" />
        )}
        <span
          className={`relative inline-flex h-2 w-2 rounded-full ${connected ? 'bg-emerald-400' : 'bg-zinc-600'}`}
        />
      </span>
      {connected ? 'WS 已连接' : 'WS 未连接'}
    </div>
  )
}

/** LLM 未配置琥珀横幅：自动决策暂停，附配置入口 */
function LlmMissingBanner({ onOpenConfig }: { onOpenConfig: () => void }) {
  return (
    <div
      role="alert"
      className="flex items-center gap-3 border-b border-amber-300/30 bg-amber-300/10 px-5 py-2 text-xs text-amber-300"
    >
      <span>LLM 未配置：监控与手动操作可用，自动决策已暂停。</span>
      <button
        type="button"
        onClick={onOpenConfig}
        className="rounded border border-amber-300/40 px-2 py-0.5 font-medium hover:bg-amber-300/10"
      >
        前往配置 LLM API Key
      </button>
    </div>
  )
}

export default function TopBar({
  status,
  wsConnected,
  onOpenConfig,
  onChanged,
}: {
  status: StatusInfo | null
  /** WS 连接状态（指示灯绿/灰） */
  wsConnected: boolean
  /** 齿轮按钮 / 横幅入口：打开配置抽屉 */
  onOpenConfig: () => void
  /** agent 启停 / kill_switch 变更后的刷新回调（可选） */
  onChanged?: () => void
}) {
  const handleAgentToggle = async (next: boolean) => {
    if (next) await api.startAgent()
    else await api.stopAgent()
    onChanged?.()
  }
  const handleKillToggle = async (next: boolean) => {
    await api.setKillSwitch(next)
    onChanged?.()
  }

  return (
    <div className="sticky top-0 z-40">
      <header className="border-b border-white/5 bg-zinc-950/80 backdrop-blur-md">
        <div className="flex min-h-14 flex-wrap items-center gap-x-4 gap-y-2 px-5 py-2 text-sm">
          <Brand />
          <div className="h-6 w-px bg-white/10" />
          <ModeBadge mode={status?.mode} />
          {/* Agent 启停 + kill_switch（复用现有两段确认按钮，错误自展示） */}
          <div className="flex items-center gap-2">
            <AgentControl
              running={status?.agent_running ?? false}
              disabled={status === null}
              onToggle={handleAgentToggle}
            />
            <KillSwitchButton
              enabled={status?.kill_switch ?? false}
              disabled={status === null}
              onToggle={handleKillToggle}
            />
          </div>
          <div className="hidden h-6 w-px bg-white/10 lg:block" />
          <LlmStatus status={status} />
          <WsDot connected={wsConnected} />
          {status && (
            <div className="hidden text-xs text-zinc-500 2xl:block">
              uptime{' '}
              <span className="font-mono tabular-nums text-zinc-300">
                {fmtUptime(status.uptime_seconds)}
              </span>
            </div>
          )}
          {/* 右侧：配置抽屉入口 */}
          <div className="ml-auto flex items-center">
            <button
              type="button"
              aria-label="打开配置中心"
              onClick={onOpenConfig}
              className="flex items-center gap-1.5 rounded-lg border border-white/10 bg-white/5 px-3 py-1.5 text-xs text-zinc-300 transition hover:border-violet-400/50 hover:text-violet-200"
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <circle cx="12" cy="12" r="3" />
                <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
              </svg>
              配置中心
            </button>
          </div>
        </div>
      </header>
      {status?.llm_configured === false && <LlmMissingBanner onOpenConfig={onOpenConfig} />}
    </div>
  )
}
