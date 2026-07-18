/**
 * 路由表：仪表盘 / 决策时间线 / 决策详情 / 交易记录 / 配置中心。
 */
import { Navigate, Route, Routes } from 'react-router-dom'
import Layout from './components/Layout'
import ConfigPage from './pages/ConfigPage'
import DashboardPage from './pages/DashboardPage'
import RoundDetailPage from './pages/RoundDetailPage'
import RoundsPage from './pages/RoundsPage'
import TradesPage from './pages/TradesPage'

export default function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<Navigate to="/dashboard" replace />} />
        <Route path="/dashboard" element={<DashboardPage />} />
        <Route path="/rounds" element={<RoundsPage />} />
        <Route path="/rounds/:roundId" element={<RoundDetailPage />} />
        <Route path="/trades" element={<TradesPage />} />
        <Route path="/config" element={<ConfigPage />} />
        <Route path="*" element={<Navigate to="/dashboard" replace />} />
      </Route>
    </Routes>
  )
}
