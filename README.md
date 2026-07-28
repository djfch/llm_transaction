# llm-transaction

Gate.io 永续合约 LLM 自主交易 Agent（一期）。

LLM 自主分析行情、自主决策开平仓、自主设置唤醒节奏；硬性风控是代码不是 prompt，LLM 只有建议权。默认 paper 模式（真实行情模拟撮合），支持 testnet 联调与 live（需用户显式开启）。

系统边界、模块职责、决策链路与数据模型见 [项目架构文档](docs/ARCHITECTURE.md)。

## 环境要求

- Python 3.11+（推荐用 `uv python install 3.11`）
- Node.js 18+
- [uv](https://docs.astral.sh/uv/)

## 快速开始

```bash
# 后端依赖
uv sync

# 配置（真实 key 填 .env，禁止提交）
cp .env.example .env

# 运行时配置（会被程序写回，不入库；从模板复制）
cp config.example.yaml config.yaml
cp watchlist.example.yaml watchlist.yaml
cp system_prompt.example.md system_prompt.md
cp review_prompt.example.md review_prompt.md

# 运行（默认 paper 模式 + 监控后台 http://127.0.0.1:17577）
uv run python -m src.main

# 前端开发（dev server 端口 17576，默认经 proxy 连本地 17577 真实后端，需先启动后端；
# 无后端独立预览：web/.env.development 设 VITE_USE_MOCK=true）
cd web && npm ci && npm run dev
```

## 测试与检查

```bash
uv run pytest tests/ -q                     # 后端测试
uv run pytest --cov=src/risk --cov-fail-under=100   # 风控覆盖率门槛
uv run ruff check src tests scripts         # 后端 lint
cd web && npm run lint && npx tsc --noEmit && npm run test && npm run build  # 前端
```

## CI

- pre-commit：`uvx pre-commit install`；普通提交运行后端测试，暂存 Python 文件时运行 Ruff lint/format，`web/` 有变更时运行前端 lint、类型检查与构建
- GitHub Actions：push 到 `main/master` 或提交 PR 时，触发后端（ruff + pytest + 覆盖率门槛）、前端（lint + tsc + vitest + build）、e2e（前端构建 + 整机冒烟）三个 job
- 契约测试：`tests/test_contract.py`（进常规 pytest 套件），冻结前端消费的全部端点响应键/类型（与 `web/src/api/types.ts` 对齐），含密钥泄漏递归扫描护栏
- 整机冒烟：`scripts/e2e_web_smoke.py`，真实端口 + dist 静态托管 + paper reset 操作链；CI e2e job 自动运行，本地运行需先 `cd web && npm run build`

## 配置说明

- `config.yaml`、`watchlist.yaml`、`system_prompt.md`、`review_prompt.md` 为运行时文件（会被 API/程序写回），**不入库**；仓库只存 `.example` 模板，克隆后需复制（见快速开始）
- `config.yaml`：运行模式（paper/testnet/live）、风控参数、LLM provider、通知、端口；`scheduler.autostart` 控制启动后是否自动开始决策（默认 false，在监控主页点击"启动 agent"才开始）
- `config.yaml` 的 `review` 节：复盘 agent 配置——`review.enabled(每日复盘开关)` 默认 true、`review.daily_time(每日触发时间，本地 HH:MM)` 默认 03:00，保存后热生效；复盘报告与策略版本历史（含 diff 与回滚）在监控页查看；人工改策略请走监控页/PUT /api/strategy（直接编辑 system_prompt.md 会热生效但不会留下版本记录）
- **安全提示**：监控 API 目前无鉴权且为明文 HTTP。`server.host` 默认 `127.0.0.1`（仅本机可达）——**绑定 `0.0.0.0` 或任何非回环地址前须知**：同网段任何人可改配置、解 kill_switch、写入 LLM key，且密钥明文过网。对外暴露前先加鉴权（后续排期）。
- `watchlist.yaml`：允许新增仓位的合约白名单（平仓不受白名单限制）
- `system_prompt.md`：策略书，LLM 每轮决策的 system prompt，改完下一轮自动生效
- 监控前端可修改 LLM、风控、通知开关、白名单和策略 Prompt；LLM provider/model 等保存后热重建并从下一轮生效，`mode` 等构造期字段会返回 `needs_restart(需要重启的字段)`
- Gate 与 Telegram 密钥、Gate 主机、服务监听地址、审计和日志路径仍需在服务器 `.env` 或 `config.yaml` 中维护

## 手动验证脚本（scripts/，不进测试套件）

```bash
uv run python scripts/smoke_paper.py 300      # paper + mock LLM 冒烟（参数为秒数），断言审计落库
uv run python scripts/check_gateway_public.py # Gate 公共 REST 连通性（无签名）
uv run python scripts/check_feed.py           # WS 行情实连：30 秒 ticker + 全周期 K 线订阅核对
uv run python scripts/testnet_roundtrip.py    # testnet 开平仓+调杠杆闭环（需 .env 配 testnet key）
```
