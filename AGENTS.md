# AGENTS.md（项目级）

## 项目简介

Gate.io 永续合约 LLM 自主交易 Agent。后端 Python 3.11（uv 管理），前端 Vite+React+TS+Tailwind。

## 构建与测试命令

- 后端：`uv sync` 安装依赖；`uv run pytest tests/ -q` 测试；`uv run ruff check src tests` lint
- 前端：`cd web && npm ci`；`npm run dev` 开发；`npm run build` 构建
- 运行：`uv run python -m src.main`（默认 paper 模式）
- 整机冒烟：`uv run python scripts/e2e_web_smoke.py`（需先 `cd web && npm run build`；真实端口起服务验证 dist 托管与 API 操作链）
- CI：`uvx pre-commit install` 安装提交钩子；GitHub Actions 见 `.github/workflows/ci.yml`

## 目录约定

```
src/
  config.py / config_io.py   # 配置模型与读写校验
  utils.py                   # 跨层共享小工具（无业务依赖）
  gateway/                   # 交易所网关（Protocol + Gate 实现 + mock）
  market/                    # WS 行情、K线缓存、价格触发器
  memory/                    # SQLite 持久化
  risk/                      # 风控引擎（纯代码，LLM 不可绕过）
  paper/                     # 模拟撮合（实现 Gateway 同一接口）
  agent/                     # LLM 循环、Provider 抽象、工具集（交易类工具在 tool_trading）
  audit/                     # 审计溯源与日志
  scheduler/                 # 唤醒调度
  notify/                    # Telegram 通知
  server/                    # FastAPI 监控 API + WebSocket
web/                         # React 前端
tests/                       # pytest
scripts/                     # 手动验证脚本（不进测试套件）
```

## 硬性规范

1. **风控安全**：`src/risk/` 覆盖率必须 100%；任何交易类工具必须先过风控；交易所 key 只读 `.env`，永不进 API 响应/日志/前端
2. **代码体量**：单文件 ≤300 行（超 500 必拆）、函数 ≤40 行、嵌套 ≤3 层
3. **解耦**：基础设施（gateway/llm/notify）与业务策略分离；模块间经接口通信，禁止跨层直接 import 具体实现
4. **金额处理**：一律 Decimal，禁止 float
5. **注释与文档**：中文
6. **Gate API 参数**：以实现计划附录的核实结果为准，禁止猜测；"文档未找到"项必须先实测
