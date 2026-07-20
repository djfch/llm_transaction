/**
 * 路由表：AI 大脑观察舱单页 —— 仅「/」→ ConsolePage，其余路径一律重定向回「/」。
 */
import { Navigate, Route, Routes } from 'react-router-dom'
import ConsolePage from './pages/ConsolePage'

export default function App() {
  return (
    <Routes>
      <Route index element={<ConsolePage />} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  )
}
