/**
 * 全局布局：左侧导航 + 页头（WS 连接状态）+ 内容区。
 */
import { NavLink, Outlet } from 'react-router-dom'
import { useWs } from '../hooks/useWs'

const NAV_ITEMS = [
  { to: '/dashboard', label: '仪表盘' },
  { to: '/rounds', label: '决策时间线' },
  { to: '/trades', label: '交易记录' },
  { to: '/config', label: '配置中心' },
]

export default function Layout() {
  const { connected } = useWs()

  return (
    <div className="flex min-h-screen bg-slate-950 text-slate-200">
      {/* 侧边导航 */}
      <aside className="flex w-52 shrink-0 flex-col border-r border-slate-800 bg-slate-900/60">
        <div className="border-b border-slate-800 px-5 py-4">
          <h1 className="text-base font-bold text-slate-100">LLM 交易 Agent</h1>
          <p className="mt-1 text-xs text-slate-500">Gate.io 永续合约监控台</p>
        </div>
        <nav className="flex-1 space-y-1 px-3 py-4">
          {NAV_ITEMS.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) =>
                `block rounded-lg px-3 py-2 text-sm transition-colors ${
                  isActive
                    ? 'bg-sky-500/15 font-medium text-sky-400'
                    : 'text-slate-400 hover:bg-slate-800 hover:text-slate-200'
                }`
              }
            >
              {item.label}
            </NavLink>
          ))}
        </nav>
      </aside>

      {/* 主区域 */}
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-center justify-between border-b border-slate-800 px-6 py-3">
          <span className="text-sm text-slate-400">实时监控</span>
          <span className="flex items-center gap-2 text-xs text-slate-400">
            <span
              className={`inline-block h-2 w-2 rounded-full ${
                connected ? 'bg-emerald-400' : 'bg-rose-500'
              }`}
            />
            WebSocket {connected ? '已连接' : '未连接'}
          </span>
        </header>
        <main className="flex-1 p-6">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
