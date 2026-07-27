# 复盘 Agent（自进化闭环）设计 spec

日期：2026-07-27 · 状态：已获用户批准 · 可视化版本：`design_proposals/review-agent-design.html`（v2）

## 1. 目标与关键决策

为 llm-transaction 增加复盘子系统：每日复盘历史交易与决策，LLM 归因分析后可直接改写交易策略书（system_prompt.md），形成自进化闭环。

用户确认的四个关键决策：

- **核心产物**：自进化闭环（复盘结论直接影响后续交易决策）
- **改动落点**：复盘 agent 有独立 system prompt（`review_prompt.md`，与交易策略书分离），可直接改写策略书，必须版本化留痕；前端能把每轮决策/审计关联到策略版本
- **触发**：每日定时（`review.daily_time`，默认 03:00）+ API/前端手动触发
- **生效**：自动生效 + 版本可回滚

## 2. 架构

进程内 `src/review/` 子系统（与交易决策同进程、零共享可变状态）：

- `ReviewScheduler`：每分钟巡检到点触发（参照 `bootstrap._funding_loop`），`run_now()` 手动入口，`asyncio.Lock` 防重入；当日幂等以 `review_reports` 落库记录为准（重启不重复）。
- `ReviewAgent`：独立 `review_prompt.md`，复用 `LLMProvider`（`set_provider` 热替换），多轮工具调用循环（≤12 轮），最终文本即复盘报告。
- `ReviewToolRegistry`：8 个工具（见 §3），无任何交易工具、不持有 Gateway 引用。
- `StrategyStore`：策略版本管理。校验 → 临时文件 → 原子替换 system_prompt.md → 版本落库；回滚 = 写回历史内容 + 记 `created_by='rollback'` 新版本。人工经 API 改策略同走此入口（`created_by='human'`）。启动时播种 v1。
- 生效机制：`PromptLoader` mtime 热重载，新版本下一轮决策自动生效，零改动复用。
- 统一审计：复盘轮以 `wake_source='review'` 创建 audit_rounds，全部工具调用落 audit_tool_calls。

## 3. 工具集（正式清单）

7 个只读工具（只经 Repo/AuditTrail 查询）：

| 工具 | 参数 | 返回 |
| --- | --- | --- |
| `get_review_stats` | `start_ts`/`end_ts` 必填；`strategy_md5`、`contract` 可空 | 平仓笔数、总盈亏、胜率、盈亏比、平均盈/亏、最大单笔亏损、各合约分布 |
| `list_decision_rounds` | `start_ts`/`end_ts` 必填；`strategy_md5` 可空；`limit` 默认 20 ≤100 | round_id、wake_source、strategy_md5、一行摘要、error、时间 |
| `get_decision_detail` | `round_id` 必填；`max_chars` 默认 4000 | 决策摘要 + llm_raw（截断）+ wake_source + strategy_md5 |
| `get_tool_call_chain` | `round_id` 必填 | 按 seq 排序：tool、args、risk_verdict、risk_reason、result 摘要（每条截断 500 字符）、duration_ms |
| `list_trades` | `start_ts`/`end_ts` 必填；`contract`/`source` 可空；`limit` 默认 50 ≤200 | 时间、合约、size、price、fee、pnl、source、round_id |
| `get_round_context` | `round_id` 必填；`max_chars` 默认 4000 | audit_rounds.context_snapshot（截断） |
| `get_strategy_versions` | `version_id` 可空 | 空=版本列表+当前全文；指定=该版本全文 |

1 个写工具（唯一出口）：

| 工具 | 参数 | 行为 |
| --- | --- | --- |
| `submit_strategy_revision` | `new_prompt_md`、`reason` 均必填 | 交 StrategyStore 校验；通过 → 新版本 vN；拒绝 → 原因列表（原文件不动），LLM 可修正后重试 |

## 4. 输出与修订协议

- 复盘报告 = 工具循环结束后 LLM 的最终文本，直接落库，无需 JSON 解析。
- 策略修订 = 调用 `submit_strategy_revision`；不调用即"无需调整"（默认状态，避免为改而改）。
- 全文重写（new_prompt_md 为策略书完整新文本），diff 由服务端生成（difflib.unified_diff）、前端着色展示。

## 5. 数据模型

新表（db.py 幂等迁移）：

```
strategy_versions: id, content, md5, created_by(human/review_agent/rollback), reason, report_id NULL, created_at
review_reports:    id, period_start, period_end, stats_json, report_md, strategy_action(none/rewrite),
                   new_version_id NULL, error NULL, created_at
```

迁移列：`decisions`、`audit_rounds` 各加 `strategy_md5`（策略书原文 md5）。既有 `decisions.strategy_version` 为"策略书+工具说明段"拼装 md5，两者并存、语义不同。`PromptLoader` 增加 `body_md5()`，`DecisionLoop` 落库时写入；不回填历史数据。

版本 ↔ 决策关联：前端按 `strategy_md5` join 版本表，每轮决策显示「策略 vN · 来源」；`get_review_stats` 按此列把成交 join 到策略版本统计。

## 6. 统计口径（代码算，LLM 只看不算）

- 统计样本 = `trades.source ∈ {llm_close, tpsl_close, user_close, liquidation}` 的平仓成交。
- 胜率 = 盈利笔数（pnl>0）/ 样本笔数；盈亏比 = 总盈利 / |总亏损|（总亏损为 0 时为 null）。
- 金额一律 Decimal，Python 侧合计（沿用 daily_stats 反浮点先例）。
- 按策略过滤：trades.round_id join decisions.round_id；无 join 匹配的成交不参与按策略统计。

## 7. 安全不变量（代码强制）

1. ReviewToolRegistry 无任何交易工具、不持有 Gateway 引用。
2. 写前校验：strip 后 ≥100 字符、≤32KB、与当前版本有差异；任一不过拒绝且原文件不动。
3. 复盘失败只落 error 记录 + Telegram 告警，交易决策循环零感知。
4. 统一审计 + 密钥边界：复盘工具调用全落审计；不接触 .env；新端点进契约测试密钥扫描。
5. 通知摘要经 `html.escape` 且 ≤500 字符（Telegram parse_mode=HTML 防注入）。

## 8. API 与前端

新端点：`GET /api/review/reports(+{id})`、`POST /api/review/run`（409/503）、`GET /api/strategy/versions(+{id})`、`GET /api/strategy/diff?from=&to=`、`POST /api/strategy/rollback/{id}`。`/api/rounds(+{id})` 响应补 `strategy_md5`。`PUT /api/strategy` 改走 StrategyStore，响应保持 PlainText 原文（契约零破坏）。`_RUNTIME_KEYS` 加 `review.enabled`、`review.daily_time`。

前端（零新 npm 依赖）：ConsolePage 新增复盘报告面板；决策轮时间线与详情加策略版本标签；策略编辑页加版本历史侧栏（列表/diff/回滚）。

## 9. 配置

```yaml
review:
  enabled: true
  daily_time: "03:00"   # 本地时间，HH:MM
```

`review_prompt.example.md` 入库模板，`review_prompt.md` 运行时文件不入库（同 system_prompt.md 约定）。LLM provider 复用现有配置。

## 10. YAGNI 边界（一期不做）

- 不回捞历史 K 线喂复盘 LLM；不做周报/月报聚合、不做单笔即时复盘。
- 不做复盘独立模型配置、不做多策略 A/B。
- 不给复盘 agent 任何交易/调度/笔记写工具。
