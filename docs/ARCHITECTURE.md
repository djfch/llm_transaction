# llm-transaction 项目架构

本文描述当前代码的系统边界、模块职责和关键运行链路。实现发生变化时，应以代码与自动化测试为准，并同步更新本文。

## 1. 架构目标与核心约束

系统让 LLM 读取行情和账户上下文、调用受控工具并安排下一次唤醒，但不把最终交易权限直接交给 LLM。

```mermaid
flowchart LR
    LLM["LLM：分析并提出工具调用"] --> Decision["DecisionLoop：编排决策轮"]
    Decision --> Tools["ToolRegistry：执行受控工具"]
    Tools -->|"下单 / 改单"| Risk["RiskEngine：完整风控"]
    Risk -->|"Allow / Deny + reasons"| Tools
    Tools -->|"Allow 后执行下单 / 改单（按需设置杠杆）"| Gateway["运行时 Gateway 实例"]
    Tools -->|"撤单"| Gateway
    Gateway --> Execution["外部 Gate.io 或进程内模拟撮合"]
    Decision --> Audit["AuditTrail：决策轮与工具审计"]
    Tools --> Repo["Repo：订单、成交和笔记"]
    Audit --> Repo
    Tools --> Decision
    Decision --> LLM
```

系统不可违反的核心约束：

- 所有 LLM 下单和改单意图必须先经过 `RiskEngine(风控引擎)`；设置杠杆也必须通过代码中的 `risk.max_leverage(最大杠杆)` 上限，LLM 不能绕过这些代码约束。
- `GATE_API_KEY(交易所访问密钥)`、`GATE_API_SECRET(交易所签名密钥)` 只从 `.env` 读取，不进入 API 响应、日志或前端。
- 金额、价格和数量在后端业务链路使用 `Decimal(十进制定点数)`；SQLite 以 TEXT 保存其字符串，避免二进制浮点误差。
- `Gateway(交易网关接口)` 隔离业务层与 Gate SDK；paper、testnet、live 对上层暴露同一组交易语义。
- 被工具层标记为 `is_close(平仓或减仓)` 的意图会豁免多数开仓限制；限价减仓仍受委托价偏离规则约束。当前改单方向识别存在已知边界，见“风控边界”。
- LLM 决策轮、工具调用、风控结论和决策异常进入统一审计；人工 API 操作保留对应业务记录，但当前不写入 LLM 工具审计链。

## 2. 系统全景

后端是一个 asyncio 单进程应用。`src/bootstrap.py` 作为组合根创建依赖，并并发运行行情源、决策调度器、FastAPI、WebSocket 事件泵和 paper 资金费任务。

```mermaid
flowchart LR
    User["用户 / 浏览器"] --> Web["React 监控台"]

    subgraph Runtime["bootstrap.py 组装的 Python asyncio 单进程"]
        API["接口层<br/>FastAPI 路由 + ServerDeps"]
        Realtime["实时推送<br/>event_queue + WebSocket Hub"]
        Agent["决策子系统<br/>WakeupScheduler + DecisionLoop<br/>ContextBuilder + ToolRegistry + RiskEngine"]
        Market["行情与触发子系统<br/>PriceSource + CandleCache<br/>Ticker 处理器 + TriggerManager + paper 资金费"]
        Gateway["运行时交易网关<br/>PaperGateway / GateRestGateway"]
        State["状态与审计<br/>Repo + AuditTrail"]
        Provider["运行时 LLM Provider 实例"]
        Notify["Notifier<br/>告警通知"]
    end

    Web -->|"HTTP /api"| API
    Web <-->|"/ws 实时事件"| Realtime
    API -->|"启停 Agent / 手动平仓与撤单"| Agent
    API -->|"账户查询 / paper reset"| Gateway
    API -->|"状态与历史查询"| State

    Agent -->|"读取 K 线 / 管理预警"| Market
    Market -->|"价格预警 wake_now"| Agent
    Agent -->|"读取账户 / 受控交易"| Gateway
    Agent -->|"业务记录与审计"| State
    Agent -->|"round_start / round"| Realtime
    Agent <-->|"chat / tool calls"| Provider
    Agent -->|"连续失败告警"| Notify

    Market -->|"REST 回补 / paper 撮合与资金费"| Gateway
    Market -->|"ticker 事件"| Realtime
    State --> DB[("SQLite<br/>data/agent.db")]
    State --> Snapshots["logs/audit JSON 快照"]

    Market <-->|"MarketFeed：WebSocket"| Gate["Gate.io"]
    Gateway <-->|"交易与 K 线 REST"| Gate
    Provider -->|"AnthropicProvider / OpenAICompatProvider / OpenAIResponsesProvider"| LLMAPI["外部 Anthropic / OpenAI API"]
    Provider -.->|"MockProvider"| MockLLM["进程内测试响应"]
    Notify --> Telegram["Telegram API"]
```

开发环境中，Vite 运行在 `17576` 端口，并把 `/api` 和 `/ws` 代理到 FastAPI 的 `17577` 端口。存在 `web/dist` 时，FastAPI 会在根路径直接托管前端静态文件。

## 3. 目录与模块职责

| 路径 | 职责 | 主要边界 |
| --- | --- | --- |
| `src/main.py` | 加载 `.env`、配置和日志，启动应用 | 只处理入口与顶层生命周期 |
| `src/bootstrap.py` | 创建并连接全部运行时组件 | 项目的组合根；具体依赖在此选择 |
| `src/config.py` / `src/config_io.py` | 配置模型、读取、校验与安全写回 | 运行时 YAML 与密钥环境变量分离 |
| `src/gateway/` | 交易所领域模型、Protocol、Gate REST 与 mock | 业务层不直接依赖 Gate SDK 类型 |
| `src/market/` | WebSocket 行情、K 线缓存、价格触发器、私有成交订阅（testnet/live） | 行情接入与交易决策解耦 |
| `src/paper/` | 模拟账户、撮合、资金费与强平 | 实现与真实网关相同的接口 |
| `src/risk/` | 无状态风控引擎与纯函数规则 | 下单/改单调用 RiskEngine；撤单不调用；设置杠杆由 Agent 工具层独立校验上限 |
| `src/agent/` | 上下文构造、LLM Provider、决策循环、工具和成交落库/对账 | LLM 只能经 ToolRegistry 影响系统 |
| `src/scheduler/` | 定时、手动和价格告警唤醒 | 保证同一时间最多执行一轮决策 |
| `src/review/` | 复盘 LLM 循环、只读工具集、策略版本管理与每日调度 | 无任何交易工具、不持有 Gateway；唯一写出口经 StrategyStore 校验 |
| `src/research/` | 前瞻研报、数据工具、逐标的市场快照、因果链与三盘调度 | 每轮冻结白名单；完整输入快照留后端，逐合约结论供执行上下文和方向闸门读取 |
| `src/memory/` | SQLite 建表、模型与 Repo | 业务层不直接写 SQL |
| `src/audit/` | 决策轮与工具调用审计 | 保存输入、输出、风控结论和耗时 |
| `src/server/` | FastAPI、WebSocket、静态文件与依赖容器 | 通过 `ServerDeps(服务依赖容器)` 调用运行时能力 |
| `src/notify/` | Telegram 等通知出口 | 通过接口与 Agent 解耦 |
| `web/src/api/` | HTTP/WS 客户端、契约类型与字段适配 | 页面不直接处理后端原始字段 |
| `web/src/pages/` / `web/src/components/` | 单页监控台与业务组件 | 展示状态并触发受控 API 操作 |
| `tests/` | 后端单元、集成、契约与回归测试 | `src/risk/` 覆盖率必须为 100% |
| `scripts/` | 联调、整机冒烟、Git 钩子辅助与部署脚本 | 手动验证脚本不进入常规 pytest 套件 |

依赖方向以“领域接口优先”为原则：

```mermaid
flowchart TB
    UI["web UI"] -->|"HTTP / WebSocket"| Server["server"]
    Bootstrap["bootstrap 组合根"] -->|"创建并注入"| Server
    Bootstrap -->|"创建并注入"| Agent["agent"]
    Bootstrap -->|"选择实现"| GatewayImpl["GateRestGateway / PaperGateway"]
    Bootstrap -->|"创建并注入"| Market["market"]
    Bootstrap -->|"创建并注入"| Scheduler["scheduler"]
    Bootstrap -->|"创建并注入"| Repo["Repo"]
    Server --> ServerDeps["ServerDeps"]
    Agent --> Gateway["Gateway Protocol"]
    Agent --> Repo
    Agent --> Risk["RiskEngine"]
    ServerDeps --> Gateway
    ServerDeps --> Repo
    GatewayImpl -.-> Gateway
    Market --> Gateway
    Scheduler --> Agent
```

图中实线表示源码依赖或运行时调用，标有“创建并注入”的箭头表示组合根的组装行为，虚线表示接口实现关系。`server` 不应直接导入 Agent、调度器或行情源的具体实现；写操作通过 `ServerDeps` 注入的回调进入业务层。`bootstrap` 可以依赖具体实现，因为它负责最终组装。

## 4. 启动与关闭链路

```mermaid
sequenceDiagram
    participant Main as main.py
    participant Boot as bootstrap.py
    participant DB as SQLite
    participant Source as PriceSource
    participant Scheduler as WakeupScheduler
    participant API as FastAPI

    Main->>Main: 加载 .env / config.yaml / watchlist.yaml
    Main->>Boot: build_app(settings, watchlist)
    Boot->>DB: 打开数据库、建表和幂等迁移
    Boot->>Boot: 创建 Gateway、行情、缓存、触发器、Agent
    Boot->>API: 注入 ServerDeps 并创建应用
    Main->>Boot: run_app(context)
    Boot->>Source: 启动行情源
    opt scheduler.autostart(启动即决策) = true
        Boot->>Scheduler: 启动调度
    end
    Boot->>API: 启动 Uvicorn
    Note over Boot,API: Ctrl+C 或 duration 到期
    Boot->>Scheduler: 停止并等待当前决策轮收尾
    Boot->>Source: 停止行情源
    Boot->>API: 请求退出
    Boot->>DB: 关闭连接
```

`scheduler.autostart(启动后自动决策)` 默认是 `false`。此时行情与监控服务会正常启动，用户在前端点击“启动 agent”后，调度器立即以 `manual_start(手动启动)` 原因唤醒第一轮。

## 5. 单轮决策链路

一轮决策可由定时器、用户启动 Agent 或价格预警触发。决策轮内到达的外部抢醒只保留最后一个原因，并在轮末补一次；定时器到期事件则丢弃，避免同时运行多个 LLM 决策轮。

```mermaid
sequenceDiagram
    participant WakeSource as 唤醒源
    participant Scheduler as WakeupScheduler
    participant Callback as bootstrap.on_wake
    participant Queue as event_queue
    participant Decision as DecisionLoop
    participant Context as ContextBuilder
    participant LLM as LLM Provider
    participant Tools as ToolRegistry
    participant Risk as RiskEngine
    participant Gateway as Gateway
    participant Repo as Repo
    participant Audit as AuditTrail
    participant Hub as WebSocket Hub

    WakeSource->>Scheduler: wake_now(wake_source) 或定时器到期
    Scheduler->>Callback: on_wake(wake_source)
    Callback->>Decision: run_once(wake_source)
    Decision->>Audit: begin_round(创建审计轮)
    Decision->>Queue: round_start(审计行已可查询)
    Decision->>Context: 构建账户、持仓、行情、笔记、成交上下文
    Context-->>Decision: context_text(完整上下文)
    Decision->>Audit: record_context(记录上下文)
    loop 最多 max_turns(最大工具轮次)
        Decision->>LLM: system prompt + messages + tools
        LLM-->>Decision: 文本或 tool_calls(工具调用)
        Decision->>Audit: record_llm_raw(实时累计原始响应)
        Decision->>Tools: execute(name, args)
        alt 下单或改单
            Tools->>Risk: check(trade_intent)
            alt 风控允许
                Risk-->>Tools: Allow
                Tools->>Gateway: 下单（必要时先设置杠杆）/ 改单
            else 风控拒绝
                Risk-->>Tools: Deny + reasons(拒绝理由)
            end
        else 撤单
            Tools->>Tools: 校验撤单参数
            Tools->>Gateway: 撤单
        else 数据、笔记、预警或调度工具
            Tools->>Gateway: 按工具需要读取账户 / 合约
            Tools->>Repo: 按工具需要查询历史或写入笔记
            Note over Tools: 预警写入内存 TriggerManager（不持久化，重启即失效）
            Tools->>Scheduler: 按工具需要设置下次唤醒
        end
        Tools-->>Decision: ToolOutcome(工具结果)
        Decision->>Audit: record_tool_call(参数、结果、风控与耗时)
        Decision->>LLM: tool_result(工具结果)
    end
    Decision->>Repo: 保存 decision(决策记录)
    Decision->>Audit: end_round(结束审计并写 JSON 快照)
    Decision->>Gateway: paper 模式泄放成交缓冲
    Decision-->>Callback: RoundResult
    Callback->>Queue: round(决策结束)
    Queue->>Hub: pump_events(广播事件)
```

当前决策 Agent 有 13 个工具，分为四类：

- 读取：`get_market_data(读取行情)`、`get_history(读取历史)`。
- 计算：`calc(精确计算)`。
- 交易：`place_order(下单)`、`update_tpsl(更新止盈止损)`、`amend_order(改单)`、`cancel_order(撤单)`。
- 自主管理：`set_price_alert(设置价格预警)`、`cancel_price_alert(取消价格预警)`、`set_next_wakeup(安排下次唤醒)`、`write_note(跨轮笔记)`、`update_trade_plan(全文覆盖更新交易计划)`、`clear_trade_plan(清空交易计划)`。

交易计划为全局唯一一份自由文本（`trade_plan` 单行表，≤4000 字符）：多合约想法写在同一份里，更新即全文覆盖；当前计划每轮随决策上下文注入（原文逐行加引用前缀定界）；历史不单独留表——每轮审计上下文快照已冻结当轮计划原文，复盘可溯。计划是建议性记录：不自动下单、不触发唤醒，实际下单照常过 `RiskEngine`。

价格预警线以内存为唯一存储（`TriggerManager(触发器管理器)`），进程重启即失效，由 LLM 在后续决策轮按需重设；`set_price_alert` 对同合约、同方向、同价格（Decimal 数值相等）的重复设置直接回复"已设置"，不创建第二条。当前预警线会随决策上下文注入 LLM，并在触发时以 `price_trigger(价格触发)` 原因抢醒调度器。


工具异常会被转换为可读结果返回给 LLM，使其有机会在本轮修正参数；工具内部异常仍写日志，但不会直接击穿整轮循环。
### 研报链路

研报 Agent 每轮先冻结 `watchlist.contracts(白名单合约)`，预注入宏观日历、指标、快讯、
历史判断与待验证因果链。LLM 必须对每个白名单合约恰好调用一次
`get_research_market_data(获取研报市场数据)`；工具固定返回 `4h(K线)` 与
`1d(K线)`，指标使用最多 200 根已收盘历史预热，原始 K 线根数由
`limit(K线根数)` 控制。最终输出的 `asset_views(逐标的结论)` 必须与白名单及工具
调用集合完全相等，否则整份研报失败。

`schema_version(结构版本)=2`报告头与全部逐标的结论在同一事务中保存，逐标的行同时冻结
`market_context_json(当轮市场输入快照)` 和 `verify_result(后续验证占位)`。执行
上下文只注入当前白名单合约对应的结论；方向闸门也只查询订单合约，且纯结构延续只作
软参考。生产基线从未部署研报结构，因此当前实现不提供 v1 迁移或展示兼容；若启动时
检测到旧研报表，会明确拒绝启动并要求人工备份、重建。


复盘 agent 使用独立注册表：7 个查询工具（`get_review_stats`、`list_decision_rounds`、`get_decision_detail`、`get_tool_call_chain`、`list_trades`、`get_round_context`、`get_strategy_versions`）只经 `Repo` 与审计表查询历史，`calc(精确计算)` 只处理数学表达式；`submit_strategy_revision(提交策略修订)` 是唯一业务写出口，提交的策略书新文本经 `StrategyStore` 校验后才生效。该注册表不含任何交易工具。

### 复盘链路

复盘链路与交易决策链路同进程但解耦，不复用上述唤醒调度。距上次复盘满 `review.interval_days` 天且到达触发时刻（`review.daily_time`，巡检循环每分钟检查）或经 `POST /api/review/run` 手动触发后，`ReviewAgent` 以 `wake_source='review'` 开启一条审计轮：先构造中文简报（复盘区间——最近 interval_days 天、当前策略书全文、代码侧预统计）并在调用 LLM 前回填到上下文快照，随后进行最多 12 轮只读工具调用，工具循环结束后的最终文本落库为复盘报告。若 LLM 调用 `submit_strategy_revision`，`StrategyStore` 校验（strip 后 ≥100 字符、UTF-8 ≤32KB、与当前版本有差异）通过后经临时文件原子替换 `system_prompt.md` 并落新版本，下一轮决策由 `PromptLoader` mtime 热重载自动生效。复盘任何失败只落 error 报告、审计轮 error 并告警，绝不向上抛，交易决策循环零感知。

## 6. 风控边界

`RiskEngine` 对同一输入产生确定性结果，并汇总全部拒绝理由。当前规则如下：

| 配置或规则 | 具体含义 | 开仓 | 平仓/减仓 |
| --- | --- | --- | --- |
| `watchlist.contracts(交易白名单)` | 允许新开仓的合约集合 | 必须命中 | 豁免 |
| `risk.kill_switch(开仓总闸)` | 紧急禁止新增风险 | 开启时拒绝 | 豁免 |
| `risk.max_position_pct(单笔交易意图名义价值上限)` | 单笔意图名义价值相对账户权益上限；配置键名称沿用 `max_position_pct` | 校验 | 豁免 |
| `risk.max_total_position_pct(总持仓权益占比上限)` | 当前持仓加本单后的总名义价值上限 | 校验 | 豁免 |
| `risk.max_leverage(最大杠杆)` | 请求杠杆倍数上限 | 校验 | 豁免 |
| `risk.daily_loss_limit(日亏损比例上限)` | 已实现与未实现亏损合计触线后只平不开 | 校验 | 豁免 |
| `risk.max_orders_per_day(日开仓单数上限)` | 当日可新增的开仓订单数量 | 校验 | 豁免 |
| `risk.max_deviation(委托价偏离上限)` | 限价相对标记价的最大偏离 | 校验 | 限价减仓仍校验；`close=true(全部平仓)` 按市价执行 |

当前风控实现存在两个明确边界：

- `risk.max_position_pct` 当前只检查本次意图的名义价值，不聚合同一合约的已有仓位；同一合约多次加仓主要由 `risk.max_total_position_pct(总持仓权益占比上限)` 兜底。若产品要求“单合约最终净仓上限”，应另行修改规则、前端标签并补回归测试。
- `amend_order(改单)` 当前仅根据新订单方向是否与持仓相反来推断 `is_close`，没有校验反向数量是否超过现有仓位。在 paper 模式中，超量反向改单成交后可能先平仓再翻向开仓，却仍使用平仓豁免。修复时应先补超量反向改单回归测试，再收紧方向与数量判定。

若 LLM 连续失败次数达到 `llm.max_consecutive_failures(最大连续失败次数)`，决策循环会开启风险锁并通过通知出口告警。LLM 未配置时会跳过决策，但 paper 成交缓冲仍会被处理，避免非 LLM 成交丢失。

## 7. 运行模式与外部依赖

| `mode(运行模式)` | 行情来源 | 交易执行 | 典型用途 |
| --- | --- | --- | --- |
| `paper(模拟模式)` | Gate 公共行情；测试可注入手动行情 | `PaperGateway(模拟撮合网关)` | 默认开发、回归与策略观察 |
| `testnet(测试网模式)` | Gate 测试网行情 | `GateRestGateway(真实 REST 网关)` 指向测试网 | 带密钥的交易闭环联调 |
| `live(实盘模式)` | Gate 正式行情 | `GateRestGateway(真实 REST 网关)` 指向正式环境 | 用户显式开启后的真实交易 |

paper 模式会处理滑点、挂单成交、资金费和强平等模拟账户行为；成交经 `FillPersister(统一成交写入入口)` 三路（行情即时 drain / 手动平仓 / 轮末兜底 drain）在同一把锁内落库，天然无双计。testnet/live 共用 `GateRestGateway`，根据 mode 选择不同主机；成交不再由下单响应推断，改由 `ExchangeFillSync(成交同步器)` 对账：私有 WS（usertrades/autoorders/liquidates）推送幂等落库，启动与断线重连按水线重叠窗口补漏，另有 5 分钟一次幂等安全网兜底 gatews 静默重连窗口，平仓 pnl 经 position_close 延迟回填（字段均以 testnet 实测校准，见 `src/agent/fill_sync.py` 模块说明与 `scripts/verify_private_feed.py`）。testnet 的 WS 地址须显式配置 `gate.testnet_ws_host(测试网WS地址)`（SDK 内置地址已失效，须与 settle 匹配）；两种模式读取相同名称的 `GATE_API_KEY(交易所访问密钥)`、`GATE_API_SECRET(交易所签名密钥)`，变量值必须与目标环境匹配。

LLM Provider 通过统一接口接入：

- `AnthropicProvider(Anthropic 模型适配器)`。
- `OpenAICompatProvider(OpenAI 兼容接口适配器)`。
- `OpenAIResponsesProvider(OpenAI Responses API 适配器)`。
- `MockProvider(测试适配器)`。

LLM key 或模型配置更新后会尝试热重建 Provider；成功则下一轮生效，失败会保留旧 Provider 并返回错误。`mode(运行模式)`、Gate 主机和服务监听地址等构造期配置需要重启。

## 8. HTTP、WebSocket 与前端

FastAPI 路由按职责拆分：

- `routes_status.py`：运行状态、账户、持仓、决策轮、成交、权益和笔记。
- `routes_plans.py`：当前交易计划（`GET /api/plan`，全局唯一一份，无计划时 content 为空串）。
- `routes_config.py`：配置、策略、白名单、LLM 密钥状态、兼容密钥写入和 `kill_switch(开仓总闸)`。
- `routes_credentials.py`：LLM 凭证新增、编辑与删除；凭证定义写入 `config.yaml`，key 明文只写服务器 `.env`。
- `routes_review.py`：复盘报告分页列表与详情（`GET /api/review/reports`、`GET /api/review/reports/{id}`）、手动触发最近 `review.interval_days` 天或指定历史区间复盘（`POST /api/review/run`，回调未接线或 LLM 未配置 503、复盘进行中 409、区间非法 422）、策略版本列表与详情（`GET /api/strategy/versions`、`GET /api/strategy/versions/{id}`）、两版本 unified diff（`GET /api/strategy/diff?from=&to=`，纯文本）与回滚（`POST /api/strategy/rollback/{id}`，未接线 503、版本不存在 404）。
- `routes_trading.py`：未成交挂单、手动撤单、手动平仓、paper 重置、Agent 启停和 K 线。
- `ws.py`：广播 `ticker(实时价格)`、`round_start(决策开始)` 和 `round(决策结束)` 等事件。

`PUT /api/strategy` 通过 `StrategyStore` 保存并记录策略版本，与复盘改写使用同一路径；strip 后不足 100 字符或 UTF-8 体积超 32KB 返回 422（detail 为全部未过原因），仅“与当前版本无差异”视为幂等成功（不产新版本）；响应契约为 `PlainText` 原文。

前端是单页控制台：

```mermaid
flowchart LR
    Console["ConsolePage"] --> Hooks["useApiData / useWs"]
    Hooks --> Client["web/src/api"]
    Client -->|"字段适配"| API["FastAPI /api"]
    Client <-->|"实时事件"| WS["/ws"]
    Console --> Panels["账户 / 持仓 / 挂单 / 风控 / K线 / 决策 / 笔记 / 成交"]
```

`web/src/api/http.ts` 是后端原始响应到前端模型的集中适配层，例如把 `created_at(Unix 秒时间)` 转为 ISO 时间，把 Decimal 字符串转为展示用 number。页面组件不应重复这些转换。

当前监控 API 默认无鉴权且使用 HTTP，默认仅监听 `127.0.0.1`。在加入鉴权、TLS 和访问控制前，不应直接绑定公网或不可信局域网地址。

## 9. 持久化与审计模型

默认数据库为 `data/agent.db`，使用 SQLite WAL 模式。模型每返回一轮响应，`AuditTrail(审计追踪器)` 会先实时更新 `audit_rounds.llm_raw(模型响应审计流)`，再允许执行该轮工具。真实 provider 的每次已收到响应都包装为单行 JSON，携带 `status(接受状态)`：`accepted(已接受并可执行)` 或 `rejected(解析失败且工具未执行)`；原始供应商正文保存在信封内，前端只让 accepted 响应消费工具审计结果，避免重试失败响应伪造执行链。rejected 响应即使没有可见正文也保留状态与拒绝原因；“本轮结论”只从无错误审计轮的 accepted 响应提取。研报的内部超时、退避取消和外部任务取消也会在退出前回收已经收到的响应。每轮结束时还会把完整记录写入 `logs/audit/round_<round_id>.json`，形成 SQLite 与 JSON 快照双写。

| 表 | 具体含义 | 关键字段 |
| --- | --- | --- |
| `decisions(决策记录)` | 每轮最终决策摘要与 LLM 原始输出 | `round_id(决策轮标识)`、`wake_source(唤醒来源)`、`strategy_md5(策略书原文摘要)` |
| `orders(订单记录)` | 本地下单记录，以及已显式同步的改单/撤单状态 | `side_size(带方向张数)`、`is_close(是否平仓)`、`trade_source(成交来源标记)` |
| `trades(成交记录)` | 成交、手续费和已实现盈亏 | `source(成交来源)`、`pnl(已实现盈亏)`、`exchange_trade_id(交易所成交ID幂等键)` |
| `notes(Agent 笔记)` | 跨决策轮传递的判断要点 | `content(笔记正文)` |
| `wakeup(未接线的唤醒记录)` | 表和 Repo 写入方法存在，但当前调度器和工具不读写此表 | `scheduled_at(计划时间)`、`source(来源)` |
| `audit_rounds(决策轮审计)` | prompt、上下文、原始输出、异常和耗时边界 | `prompt_md5(策略版本摘要)`、`strategy_md5(策略书原文摘要)`、`error(异常)` |
| `audit_tool_calls(工具审计)` | 每次工具调用的参数、风控、结果和耗时 | `risk_verdict(风控结论)`、`duration_ms(耗时毫秒)` |
| `strategy_versions(策略书版本)` | 策略书全文版本化留痕（人工保存、复盘改写与回滚同走此表） | `created_by(版本来源)`、`md5(策略书原文摘要)`、`report_id(关联复盘报告)` |
| `research_reports(研报报告头)` | 保存总览、跨标的观察、全局风险与失败信息；当前协议固定为 v2 | `schema_version(结构版本)`、`summary(研报总览)`、`cross_market_view(跨标的观察)` |
| `research_asset_views(研报逐标的结论)` | 一个报告内每个合约唯一，保存结论、输入快照与验证占位 | `contract(合约)`、`basis_type(依据类型)`、`market_context_json(市场输入快照)`、`verify_result(验证结果)` |
| `review_reports(复盘报告)` | 每次复盘的区间统计、报告全文与策略动作 | `period_start/period_end(复盘区间)`、`strategy_action(策略动作)`、`new_version_id(产生的新版本)`、`round_id(产生报告的审计轮，老报告为空串不回填)` |

注意 `decisions.strategy_version(策略版本摘要)` 与 `strategy_md5(策略书原文摘要)` 语义不同：前者是“策略书+工具说明段”拼装后的 md5（与 `audit_rounds.prompt_md5` 同值），后者是策略书原文的 md5（与 `strategy_versions.md5` 关联，供按策略版本统计）。历史数据的 `strategy_md5` 保持空串不回填；`round_id` 无法 join 到 `decisions` 的成交不参与按策略统计。

价格预警线不在上表：`TriggerManager(触发器管理器)` 以内存为唯一存储，触发即移除、重启即失效；`GET /api/alerts` 也从该内存索引读取（`active(是否有效)` 恒为 true，历史 alerts 表已废弃）。

`Repo(存取仓库)` 是业务层唯一的数据库访问入口。金额和数量以 Decimal 字符串写入 TEXT 列，读取时由领域模型还原。

## 10. 配置与状态来源

| 文件或对象 | 具体含义 | 是否纳入版本控制 |
| --- | --- | --- |
| `.env` | 交易所与 LLM 密钥 | 否 |
| `config.yaml` | 运行模式、风控、调度、模型、服务等运行参数 | 否，仅跟踪模板 |
| `watchlist.yaml` | 可交易合约白名单 | 否，仅跟踪模板 |
| `system_prompt.md` | 每轮加载的策略提示词 | 否，仅跟踪模板 |
| `review_prompt.md` | 复盘 agent 的 system prompt（模板为 `review_prompt.example.md`） | 否，仅跟踪模板 |
| `research_prompt.md` | 研报 agent 的 system prompt（模板为 `research_prompt.example.md`） | 否，仅跟踪模板 |
| `Settings(运行时配置对象)` | 已校验并注入各组件的内存配置 | 进程内 |
| `ServerDeps(服务依赖容器)` | FastAPI 可调用的运行时能力与路径 | 进程内 |

前端保存配置时，服务端先合并未提交字段、执行 Pydantic 校验并安全写回。风控、调度、paper 滑点、复盘和部分 LLM 配置会同步到共享运行时对象；构造期字段会在响应中标记需要重启。`review.enabled(复盘开关)` 默认 true、`review.interval_days(复盘间隔天数)` 默认 1、`review.daily_time(到达间隔后的触发时刻，本地 HH:MM)` 默认 03:00，三者均支持热写回——复盘巡检循环每次 tick 读取运行时配置，改开关、间隔或触发时刻即时生效。

## 11. 测试与质量门禁

```mermaid
flowchart LR
    Unit["pytest 单元/回归"] --> Backend["后端 CI"]
    RiskCoverage["src/risk 100% 覆盖率"] --> Backend
    Contract["API 契约与密钥泄漏护栏"] --> Backend
    Front["ESLint + TypeScript + Vitest + build"] --> Frontend["前端 CI"]
    Smoke["真实端口 + dist + paper 操作链"] --> E2E["整机 e2e"]
```

- 后端测试覆盖配置、网关、行情、风控、Agent、持久化、API、通知和生命周期。
- `tests/test_contract.py` 冻结前端消费的端点字段和类型，并递归检查响应中不存在密钥。
- `src/risk/` 必须达到 100% 覆盖率。
- 前端使用 Vitest 验证 API 适配、页面状态和关键交互。
- `scripts/e2e_web_smoke.py` 验证生产前端静态托管与 paper 操作链。

## 12. 修改架构时的同步清单

修改下列边界时，应同步维护对应事实来源：

- 新增 LLM 工具：同时更新 `tool_schemas.py(工具描述)`、`tools.py(工具注册)`、处理函数、审计测试和本文工具清单。
- 新增复盘工具：同时更新 `src/review/tool_schemas.py(复盘工具描述)`、`src/review/tools.py(复盘工具注册)`、处理函数、审计测试和本文复盘工具清单。
- 新增交易能力：先定义风控不变量，再补 `src/risk/` 规则或明确豁免，并维持 100% 覆盖率。
- 修改 Gateway：保持 Protocol、Gate 实现、paper/mock 实现和契约测试一致。
- 修改 API：同步 `web/src/api/types.ts(前端契约类型)`、`http.ts(响应适配)` 与 `tests/test_contract.py`。
- 修改数据库：在 `db.py` 增加幂等迁移，通过 `Repo` 暴露能力，禁止业务模块直接写 SQL。
- 修改实时事件：同步后端事件生产、`ws.py` 广播和前端 `WsMessage(实时消息类型)` 消费逻辑。
- 修改启动依赖：只在 `bootstrap.py` 选择和连接具体实现，保持业务模块依赖接口。
