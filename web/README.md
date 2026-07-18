# web/ — LLM 交易 Agent 监控前端

Vite + React + TypeScript + Tailwind CSS + lightweight-charts，监控 Gate.io 永续合约 LLM 自主交易 Agent。生产模式下由 FastAPI 托管 `dist/` 产物（单端口 8080）。

## 常用命令

```bash
npm install        # 安装依赖
npm run dev        # 开发服务器（/api、/ws 自动 proxy 到 http://127.0.0.1:8080）
npm run lint       # ESLint（flat config）
npx tsc --noEmit   # 类型检查
npm run test       # Vitest 组件测试
npm run build      # 类型检查 + 产出 dist/
```

## Mock 开关（后端未就绪时可独立预览）

默认使用 `src/api/mock.ts` 的假数据，无需后端即可 `npm run dev` 预览全部页面。
接真实后端：创建 `.env.development`，写入 `VITE_USE_MOCK=false`（见 `.env.example`）。

## 目录结构

```
src/
  api/          API 客户端层（types 契约 / http 真实实现 / mock 假数据 / ws 推送 / index 开关）
  components/   通用组件（Layout、Card、MetricCard、EquityChart、PositionCard、KillSwitchButton…）
  hooks/        useApiData（数据获取）、useWs（WS 订阅）
  pages/        DashboardPage / RoundsPage / RoundDetailPage / TradesPage / ConfigPage
  pages/config/ 配置中心子组件（RiskForm、GeneralForm、WatchlistEditor、StrategyEditor、SecretsBadges、validate）
  utils/        format.ts（数字/时间格式化）
  test/         Vitest 测试（dashboard、config、trades、position、candlecard、agentcontrol）
```

约定：字段展示使用 `变量名(含义)` 格式（如 `unrealised_pnl(未实现盈亏)`）；单文件 ≤300 行；中文 UI 与注释。
