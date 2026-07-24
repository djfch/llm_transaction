# AGENTS.md（项目级）

## 项目简介

Gate.io 永续合约 LLM 自主交易 Agent。后端 Python 3.11（uv 管理），前端 Vite+React+TS+Tailwind。

## 构建与测试命令

- 后端：`uv sync` 安装依赖；`uv run pytest tests/ -q` 测试；`uv run ruff check src tests scripts` lint
- 前端：`cd web && npm ci`；`npm run dev` 开发；`npm run build` 构建
- 运行：`uv run python -m src.main`（默认 paper 模式）
- 整机冒烟：`uv run python scripts/e2e_web_smoke.py`（需先 `cd web && npm run build`；真实端口起服务验证 dist 托管与 API 操作链）
- CI：`uvx pre-commit install` 安装提交钩子；GitHub Actions 见 `.github/workflows/ci.yml`；部署说明见 `docs/DEPLOYMENT.md`

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
scripts/                     # 验证、Git 钩子辅助与部署脚本
```

运行时会被程序写回的文件（`config.yaml` / `watchlist.yaml` / `system_prompt.md`）不入库，只跟踪 `.example` 模板；克隆后按 README 快速开始复制。

## 硬性规范

1. **风控安全**：`src/risk/` 覆盖率必须 100%；新增或扩大敞口的下单/改单必须经过 `RiskEngine`，设置杠杆必须校验 `max_leverage`；LLM 工具调用须进入统一审计，人工撤单当前至少同步订单业务记录；交易所 key 只读 `.env`，永不进 API 响应/日志/前端
2. **代码体量**：单文件 ≤300 行（超 500 必拆）、函数 ≤40 行、嵌套 ≤3 层
3. **解耦**：基础设施（gateway/llm/notify）与业务策略分离；模块间经接口通信，禁止跨层直接 import 具体实现
4. **金额处理**：一律 Decimal，禁止 float
5. **注释与文档**：中文
6. **Gate API 参数**：以实现计划附录的核实结果为准，禁止猜测；"文档未找到"项必须先实测
7. **前端字段标签**：用户可见文本仅在“英文键或枚举值 + 括号内中文释义”时只保留中文；独立英文技术标识，以及括号表示计数或状态补充的文本保持原样，例如 `tool_calls（N 步）`、`null（进行中）`。内部接口字段、类型、提交值和存储键保持不变

## Git 提交规范

1. **分支**：禁止直推 main（GitHub 分支保护强制 PR + CI 全绿）。从最新 main 拉分支：`feat/xxx`、`fix/xxx`、`chore/xxx`，一个分支只做一件事，1–3 天内合回
2. **提交信息**：`type: 中文描述`（首行 ≤72 字），type ∈ `feat/fix/docs/style/refactor/perf/test/chore/ci/build/revert`；复杂改动正文分条写。本地 commit-msg 钩子强制校验（`uvx pre-commit install --hook-type commit-msg` 安装）
3. **提交时机**：commit 可以小步多次攒在功能分支上（不必每次提交都开 PR）；**PR 是功能单位——一个 PR = 一个完整功能改动**，功能齐了才开
4. **合并**：push 后开 PR，CI 三 job（backend/frontend/e2e）全绿后由**人手动确认合并**（AI 协作者只做到"CI 绿 + 改动摘要"，不得擅自合并）；squash merge，合并即删分支
5. **大改动**（跨多文件或 >100 行）：按用户全局 AGENTS §6 流程（第一性原理 → 双 subagent 对抗审查 → 回归测试 → 验证证据）
