# llm-transaction

Gate.io 永续合约 LLM 自主交易 Agent。

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
cp research_prompt.example.md research_prompt.md
cp indicator_config.example.yaml indicator_config.yaml

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

- pre-commit：`uvx pre-commit install --hook-type pre-commit --hook-type commit-msg`（一次装齐提交检查与提交信息校验）；暂存 Python 文件时运行后端测试（含风控覆盖率 100% 门槛，与 CI 一致）与 Ruff lint/format，`web/` 有变更时运行前端 lint、类型检查与构建；纯文档提交不再触发后端测试
- GitHub Actions：push 到 `main/master` 或提交 PR 时，触发后端（ruff + pytest + 覆盖率门槛）、前端（lint + tsc + vitest + build）、e2e（前端构建 + 整机冒烟）三个 job
- 契约测试：`tests/test_contract.py`（进常规 pytest 套件），冻结前端消费的全部端点响应键/类型（与 `web/src/api/types.ts` 对齐），含密钥泄漏递归扫描护栏
- 整机冒烟：`scripts/e2e_web_smoke.py`，真实端口 + dist 静态托管 + paper reset 操作链；CI e2e job 自动运行，本地运行需先 `cd web && npm run build`

## 配置说明

- `config.yaml`、`watchlist.yaml`、`system_prompt.md`、`review_prompt.md`、`research_prompt.md`、`indicator_config.yaml` 为运行时文件（会被 API/程序写回），**不入库**；仓库只存 `.example` 模板，克隆后需复制（见快速开始）
- `config.yaml`：运行模式（paper/testnet/live）、风控参数、LLM provider、通知、端口；`scheduler.autostart` 控制启动后是否自动开始决策（默认 false，在监控主页点击"启动 agent"才开始）
- `config.yaml` 的 `review` 节：复盘 agent 配置——`review.enabled(复盘开关)` 默认 true、`review.interval_days(复盘间隔天数)` 默认 1（每隔 N 天复盘最近 N 天）、`review.daily_time(到达间隔后的触发时刻，本地 HH:MM)` 默认 03:00，保存后热生效；复盘报告与策略版本历史（含 diff 与回滚）在监控页查看；人工改策略请走监控页/PUT /api/strategy（直接编辑 system_prompt.md 会热生效但不会留下版本记录）
- `config.yaml` 的 `research` 节：研报 agent（前瞻角色，独立于交易循环）配置——`research.enabled(自动研报总开关)` 默认 false、`research.max_turns(工具调用上限)` 默认 30、`research.timeout_seconds(单次超时)` 默认 900。配置中心可分别启停东京、伦敦、纽约三个开盘前 30 分钟预设，也可添加 UTC+8 固定时间及每天/三地交易日规则；保存后立即生效，错过目标分钟不补跑，手动生成始终可用。
  - 三个预设分别在东京 09:00、伦敦 08:00、纽约 09:30 当地开盘前 30 分钟执行；伦敦和纽约会随官方时区自动切换冬夏令时。官方休市日每日刷新并缓存到 `data/market_calendar_cache.json(日历缓存)`；来源不可用时优先使用旧缓存，未知工作日按交易日处理并在配置中心警告。
  - 每轮冻结 `watchlist.contracts(白名单合约)`，逐合约读取 `4h(K线)`、`1d(K线)`、EMA、ATR、量比、资金费率、持仓量变化与背离结构，并生成 `asset_views(逐标的结论)`；完整市场输入快照仅保存在后端。
  - `research.gate_enabled(闸门开关)` 开启时，只有当前订单合约对应的高置信、方向明确、未过期、数据可用、技术面不冲突且由事件/宏观/混合驱动的结论，才会在 `research.gate_max_age_hours(结论有效期)` 内硬拒反向开仓；纯 `结构延续(依据类型)` 只作软参考。
  - 手动触发走监控页研报面板「生成研报」或 `uv run python scripts/verify_research.py`；数据源密钥只存 `.env`。报告列表与详情按合约展示方向、结构、依据、技术确认、证据和风险。
- **安全提示**：监控 API 目前无鉴权且为明文 HTTP。`server.host` 默认 `127.0.0.1`（仅本机可达）——**绑定 `0.0.0.0` 或任何非回环地址前须知**：同网段任何人可改配置、解 kill_switch、写入 LLM key，且密钥明文过网。对外暴露前必须先加鉴权与 TLS，并配置访问控制。
- `watchlist.yaml`：允许新增仓位的合约白名单（平仓不受白名单限制）
- `system_prompt.md`：策略书，LLM 每轮决策的 system prompt，改完下一轮自动生效
- `indicator_config.yaml`：指标短名单（注入决策上下文的指标，≤8 个），由复盘 agent 版本化维护（也可 PUT /api/indicator_config 人工修订）；K 线图按短名单叠加主图线/副图
- 监控前端可修改 LLM、风控、通知开关、白名单和策略 Prompt；LLM provider/model 等保存后会尝试热重建，成功则从下一轮生效，失败会保留旧 Provider 并返回错误；`mode` 等构造期字段会返回 `needs_restart(需要重启的字段)`
- **多 LLM 凭证**：`config.yaml` 的 `llm.credentials` 可登记多条凭证（每条 = provider+model+max_tokens+openai_base_url+`thinking_effort(思考程度，留空跟随模型默认，可选 off/on/low/medium/high/xhigh/max)`+`api_key_env(对应 .env 中的键名)`），`agents` 节给决策 agent（trader）、复盘 agent（reviewer）与研报 agent（researcher）分别指定所用凭证。凭证定义与 key 由专用端点顺序写入 `config.yaml` 和 `.env`，跨文件不保证原子；写入或热重建失败会明确返回错误，热重建失败时旧 Provider 继续运行。key 明文永不进入 API 响应。未配置 `credentials` 时，平铺 `llm` 字段会解析为一条名为 `default` 的兼容凭证；示例见 `config.example.yaml`
- Gate 与 Telegram 密钥、Gate 主机、服务监听地址、审计和日志路径仍需在服务器 `.env` 或 `config.yaml` 中维护

## 手动验证脚本（scripts/，不进测试套件）

```bash
uv run python scripts/smoke_paper.py 300      # paper + mock LLM 冒烟（参数为秒数），断言审计落库
uv run python scripts/check_gateway_public.py # Gate 公共 REST 连通性（无签名）
uv run python scripts/check_feed.py           # WS 行情实连：30 秒 ticker + 全周期 K 线订阅核对
uv run python scripts/testnet_roundtrip.py    # testnet 开平仓+调杠杆闭环（需 .env 配 testnet key）
uv run python scripts/verify_private_feed.py  # testnet 私有 WS 成交回报 + 平仓盈亏接口字段实测（需 .env 配 testnet key）
```
