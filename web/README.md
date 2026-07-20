# web/ — LLM 交易 Agent 监控前端

Vite + React + TypeScript + Tailwind CSS + lightweight-charts，监控 Gate.io 永续合约 LLM 自主交易 Agent。生产模式下由 FastAPI 托管 `dist/` 产物（单端口 17577）。

## 常用命令

```bash
npm install        # 安装依赖
npm run dev        # 开发服务器（端口 17576，/api、/ws 自动 proxy 到 http://127.0.0.1:17577）
npm run lint       # ESLint（flat config）
npx tsc --noEmit   # 类型检查
npm run test       # Vitest 组件测试
npm run build      # 类型检查 + 产出 dist/
```

## Mock 开关（默认走真实后端）

默认走真实后端 `/api`（`npm run dev` 经 vite proxy 转发到 127.0.0.1:17577，需先启动后端）。
仅后端未就绪需独立预览时：创建 `.env.development`，写入 `VITE_USE_MOCK=true`（见 `.env.example`）。
注意：mock 下所有修改只存在于浏览器内存，刷新即复原。

## 目录结构

```
src/
  api/          API 客户端层（types 契约 / http 真实实现 / mock 假数据 / ws 推送 / index 开关）
  components/   通用组件（Card、Badge、StateHint、AgentControl、KillSwitchButton、PaperEquitySetter）
  components/console/ 观察舱面板（TopBar、AccountPanel、LiveRoundHero、KlinePanel、RoundTimeline、
                TradesTable、NotesPanel、PositionsPanel、EquityMiniChart、ConfigDrawer 等）
  hooks/        useApiData（数据获取）、useWs（WS 订阅）、useRoundFocus（决策轮定位联动）
  pages/        ConsolePage（AI 大脑观察舱单页，路由「/」）
  pages/config/ 配置表单（RiskForm、GeneralForm、WatchlistEditor、StrategyEditor、SecretsForm、validate）
  utils/        format.ts（数字/时间格式化）、candleLive / klineMarkers / conversation 纯函数
  test/         Vitest 测试（console_* 面板用例、config、secrets、agentcontrol、http、format 等）
```

约定：字段展示使用 `变量名(含义)` 格式（如 `unrealised_pnl(未实现盈亏)`）；单文件 ≤300 行；中文 UI 与注释。
