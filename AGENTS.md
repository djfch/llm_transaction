# AGENTS.md（项目级）

## 项目简介

Gate.io 永续合约 LLM 自主交易 Agent。后端 Python 3.11（uv 管理），前端 Vite+React+TS+Tailwind。

## 构建与测试命令

- 后端：`uv sync` 安装依赖；`uv run pytest tests/ -q` 测试；`uv run ruff check src tests` lint
- 前端：`cd web && npm ci`；`npm run dev` 开发；`npm run build` 构建
- 运行：`uv run python -m src.main`（默认 paper 模式）
- 整机冒烟：`uv run python scripts/e2e_web_smoke.py`（需先 `cd web && npm run build`；真实端口起服务验证 dist 托管与 API 操作链）
- CI：`uvx pre-commit install` 安装提交钩子；GitHub Actions 见 `.github/workflows/ci.yml`；CD（手动触发部署 Linux 服务器）见 `.github/workflows/cd.yml` 与 README「CD 持续部署」

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

运行时会被程序写回的文件（`config.yaml` / `watchlist.yaml` / `system_prompt.md`）不入库，只跟踪 `.example` 模板；克隆后按 README 快速开始复制。

## 硬性规范

1. **风控安全**：`src/risk/` 覆盖率必须 100%；任何交易类工具必须先过风控；交易所 key 只读 `.env`，永不进 API 响应/日志/前端
2. **代码体量**：单文件 ≤300 行（超 500 必拆）、函数 ≤40 行、嵌套 ≤3 层
3. **解耦**：基础设施（gateway/llm/notify）与业务策略分离；模块间经接口通信，禁止跨层直接 import 具体实现
4. **金额处理**：一律 Decimal，禁止 float
5. **注释与文档**：中文
6. **Gate API 参数**：以实现计划附录的核实结果为准，禁止猜测；"文档未找到"项必须先实测
7. **前端中文展示**：面向用户的字段、表头、状态和表单标签只显示中文含义，不拼接 API 或变量英文名（例如显示“开仓价”，不显示 `entry_price(开仓价)`）；内部接口字段、类型和存储键保持原名，品牌名、合约代码、模型/提供商标识、单位、工具名及原始审计数据可保留技术标识

## Git 提交规范

1. **分支**：禁止直推 main（GitHub 分支保护强制 PR + CI 全绿）。从最新 main 拉分支：`feat/xxx`、`fix/xxx`、`chore/xxx`，一个分支只做一件事，1–3 天内合回
2. **提交信息**：`type: 中文描述`（首行 ≤72 字），type ∈ `feat/fix/docs/style/refactor/perf/test/chore/ci/build/revert`；复杂改动正文分条写。本地 commit-msg 钩子强制校验（`uvx pre-commit install --hook-type commit-msg` 安装）
3. **提交时机**：commit 可以小步多次攒在功能分支上（不必每次提交都开 PR）；**PR 是功能单位——一个 PR = 一个完整功能改动**，功能齐了才开
4. **合并**：push 后开 PR，CI 三 job（backend/frontend/e2e）全绿后由**人手动确认合并**（AI 协作者只做到"CI 绿 + 改动摘要"，不得擅自合并）；squash merge，合并即删分支
5. **大改动**（跨多文件或 >100 行）：按用户全局 AGENTS §6 流程（第一性原理 → 双 subagent 对抗审查 → 回归测试 → 验证证据）
