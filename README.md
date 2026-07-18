# llm-transaction

Gate.io 永续合约 LLM 自主交易 Agent（一期）。

LLM 自主分析行情、自主决策开平仓、自主设置唤醒节奏；硬性风控是代码不是 prompt，LLM 只有建议权。默认 paper 模式（真实行情模拟撮合），支持 testnet 联调与 live（需用户显式开启）。

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

# 运行（默认 paper 模式 + 监控后台 http://127.0.0.1:8080）
uv run python -m src.main

# 前端开发
cd web && npm ci && npm run dev
```

## 测试与检查

```bash
uv run pytest tests/ -q                     # 后端测试
uv run pytest --cov=src/risk --cov-fail-under=100   # 风控覆盖率门槛
uv run ruff check src tests                 # 后端 lint
cd web && npm run lint && npx tsc --noEmit && npm run test && npm run build  # 前端
```

## CI

- pre-commit：`uvx pre-commit install`，每次 `git commit` 自动跑前后端检查
- GitHub Actions：push/PR 触发后端（ruff + pytest + 覆盖率门槛）与前端（lint + tsc + vitest + build）两个 job

## 配置说明

- `config.yaml`：运行模式（paper/testnet/live）、风控参数、LLM provider、通知、端口
- `watchlist.yaml`：可交易合约白名单（风控硬校验）
- `system_prompt.md`：策略书，LLM 每轮决策的 system prompt，改完下一轮自动生效
- 除交易所 API key 外，以上配置均可在监控前端修改（风控/调度参数保存即生效；LLM model 等标注 needs_restart 的字段重启后生效）

## 手动验证脚本（scripts/，不进测试套件）

```bash
uv run python scripts/smoke_paper.py 300      # paper + mock LLM 冒烟（参数为秒数），断言审计落库
uv run python scripts/check_gateway_public.py # Gate 公共 REST 连通性（无签名）
uv run python scripts/check_feed.py           # WS 行情实连打印 30 秒 ticker
uv run python scripts/testnet_roundtrip.py    # testnet 开平仓+调杠杆闭环（需 .env 配 testnet key）
```
