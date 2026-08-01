# AGENTS.md（项目级）

## 项目简介

Gate.io 永续合约 LLM 自主交易 Agent。后端 Python 3.11（uv 管理），前端 Vite+React+TS+Tailwind。

## 构建与测试命令

- 后端：`uv sync` 安装依赖；`uv run pytest tests/ -q` 测试；`uv run ruff check src tests scripts` lint
- 前端：`cd web && npm ci`；`npm run dev` 开发；`npm run build` 构建
- 运行：`uv run python -m src.main`（默认 paper 模式）
- 整机冒烟：`uv run python scripts/e2e_web_smoke.py`（需先 `cd web && npm run build`；真实端口起服务验证 dist 托管与 API 操作链）
- CI：`uvx pre-commit install --hook-type pre-commit --hook-type commit-msg` 一次装齐提交钩子与提交信息校验钩子；GitHub Actions 见 `.github/workflows/ci.yml`；部署说明见 `docs/DEPLOYMENT.md`

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
  review/                    # 复盘 agent：只读工具 + 策略版本化
  notify/                    # Telegram 通知
  server/                    # FastAPI 监控 API + WebSocket
web/                         # React 前端
tests/                       # pytest
scripts/                     # 验证、Git 钩子辅助与部署脚本
```

运行时会被程序写回的文件（`config.yaml` / `watchlist.yaml` / `system_prompt.md` / `review_prompt.md`）不入库，只跟踪 `.example` 模板；克隆后按 README 快速开始复制。

## 硬性规范

1. **风控安全**：`src/risk/` 覆盖率必须 100%；新增或扩大敞口的下单/改单必须经过 `RiskEngine`，设置杠杆必须校验 `max_leverage`；LLM 工具调用须进入统一审计，人工撤单当前至少同步订单业务记录；交易所 key 只读 `.env`，永不进 API 响应/日志/前端
2. **代码体量**：单文件 ≤300 行（超 500 必拆；机械门禁 `scripts/check_file_size.py`，进 pre-commit 与 CI，>500 行失败、300–500 行警告）、函数 ≤40 行、嵌套 ≤3 层（由 ruff C901/PLR0912/PLR0915 近似兜底，阈值见 pyproject.toml，路由工厂文件豁免；精确行数/嵌套深度为评审约定）
3. **解耦**：基础设施（gateway/llm/notify）与业务策略分离；模块间经接口通信，禁止跨层直接 import 具体实现
4. **金额处理**：一律 Decimal，禁止 float（评审约定，无机械 gate——float 的合法用途无法可靠区分，代码评审时重点检查金额链路）
5. **注释与文档**：中文
6. **Gate API 参数**：以实现计划附录的核实结果为准，禁止猜测；"文档未找到"项必须先实测
7. **前端字段标签**：用户可见文本仅在“英文键或枚举值 + 括号内中文释义”时只保留中文；独立英文技术标识，以及括号表示计数或状态补充的文本保持原样，例如 `tool_calls（N 步）`、`null（进行中）`。内部接口字段、类型、提交值和存储键保持不变

## 开工与范围

1. 从第一性原理思考：从真实需求、代码事实和验证结果出发；目标不明确时，先与用户讨论，不要基于臆测开工。
2. 开工先确认工作目录，阅读本文件、相关目录的 `AGENTS.md`、`README.md` 和任务涉及的设计文档；检查 `git status` 与最近提交，保护用户已有修改。
3. 以代码而非文档为事实来源。修改代码前，先阅读相关代码和最新约束，并遵循目录树中最近的那份 `AGENTS.md`。
4. 严格保持用户指定范围，保持改动聚焦，不要顺手夹带无关的重构。诊断、评审、方案讨论默认只读；只有用户要求实现时才修改。

## 根因分析与修复方案的呈现方式（面向非程序员）

项目 Owner 不写代码。分析 bug 根因或给出修复方案时，结论必须按以下方式表达：

1. **先说人话结论**：用一两句日常语言概括"到底哪里出了问题"，禁止一上来贴代码或文件路径。
2. **打比方讲链路**：用"谁做了 A，但忘了做 B"这种叙事方式描述数据/操作的流转过程；可以画简单的 mermaid 图或 ASCII 流程图辅助。
3. **区分现象与根因**：明确说明"你看到的表象是什么"和"真正的原因（在系统内部发生了什么）"，例如"不是没保存成功，而是界面没被告知去刷新"。
4. **代码细节折叠到最后一节**：文件路径、函数名、行号等只放在结论之后的"技术细节（可选看）"小节里，供偶尔核对，不作为主要叙述。
5. **修复方案同样先讲意图**：先说"打算怎么修、为什么这样修、会有什么效果"，再说具体改哪些文件。
6. **避免术语轰炸**：遇到必须用的术语（如组件、状态、接口），首次出现时用括号给出一句通俗解释。

示例（好的表达）：
> 根因：保存成功后，只有"策略全文"被重新拉取了，"版本历史列表"没人通知它刷新。新版本其实已经存进数据库了，只是界面还停在旧数据上——所以不是没存，而是没刷新。

示例（不好的表达）：
> 根因：ConfigDrawer.tsx:114 的 onSave 只调了 strategyQ.reload()，StrategyVersions.tsx:80 的 useApiData deps 为空数组，保存路径没有触发 query.reload()。

## Git 提交规范

1. **分支**：禁止直推 main（GitHub 分支保护强制 PR + CI 全绿）。从最新 main 拉分支：`feat/xxx`、`fix/xxx`、`chore/xxx`，一个分支只做一件事，1–3 天内合回
2. **提交信息**：`type: 中文描述`（首行 ≤72 字，不带 scope），type ∈ `feat/fix/docs/style/refactor/perf/test/chore/ci/build/revert`；复杂改动正文分条写。本地 commit-msg 钩子强制校验（安装命令见上方“构建与测试命令”CI 一节，一条命令装齐两阶段）
3. **提交时机**：commit 可以小步多次攒在功能分支上（不必每次提交都开 PR）；**PR 是功能单位——一个 PR = 一个完整功能改动**，功能齐了才开
4. **合并**：push 后开 PR，CI 三 job（backend/frontend/e2e）全绿后由**人手动确认合并**（AI 协作者只做到"CI 绿 + 改动摘要"，不得擅自合并）；squash merge，合并即删分支
5. **大改动**（跨多文件或 >100 行）必须执行以下流程：
   1. 修复问题时使用第一性原理：先定义用户可观察行为；再找出系统不可违反的不变量；然后把不变量转成可验证规则（单元测试、集成测试、类型约束、数据库约束、断言、CI 检查）；不接受只修表面现象的补丁
   2. 实现完成后，用两个 subagent 做对抗性审查：Subagent A 主动寻找 bug、遗漏边界、架构风险、测试缺口；Subagent B 逐条核实 A 提出的问题是否真实存在、是否可复现、是否违反需求或不变量
   3. 对真实且重要的问题，先补回归测试，再按第一性原理做最小正确修复
   4. 检查 harness 内容（pre-commit、CI 工作流、门禁脚本）是否需要同步更改
   5. 检查 README、AGENTS 文档是否需要同步更改
   6. 任何“完成、修复、通过”的表述都必须基于刚刚运行过的验证证据，不能凭感觉或 subagent 报告直接声称完成
