## 需求

1. 复盘取消「每日」概念：新增 `review.interval_days`（1-30，默认 1=保持现状），每隔 N 天复盘最近 N 天；复盘提示词去掉「每日对上一个自然日」的诱导表述，改为以简报注入的区间为准。
2. 新增 `calc` 计算器工具（执行 agent 与复盘 agent 双侧注册）：LLM 输入 `2*(3-1)^2`，工具返回 `8`。

## 改动

### 需求一：间隔复盘
- `ReviewConfig` 新增 `interval_days`（Field 1-30）；`daily_time` 语义改为「到达间隔后的触发时刻」，键名不变，老配置零迁移
- `ReviewScheduler._tick` / `run_now` 区间泛化为「最近 interval_days 天（对齐当日 00:00）」；`interval_days=1` 与原行为逐字节等价
- `review.interval_days` 进 `_RUNTIME_KEYS` 热写回，tick 即时生效
- `review_prompt.example.md` 角色定位改为「按简报给定的复盘区间（可能一天或数天）」；README / ARCHITECTURE / 前端 ReviewPanel 文案同步

### 需求二：calc 工具
- `src/utils.py` 新增 `calc_expression`：手写 tokenizer + 递归下降，全程 Decimal（prec=28），支持 `+ - * / ^`（`**` 等价）、括号、一元负号、科学计数法；防护：长度 ≤200、指数 ≤1000、括号嵌套 ≤50 层、Emax 限幅；一切错误转中文文本不抛异常
- 执行/复盘两侧 schema/handler/registry 三件套对称注册；复盘纪律补「衍生计算用 calc 不心算」

## 双 subagent 对抗审查 → 第一性原理修复（证据留痕）

实现完成后并行发起 CodeReview（常规）+ GeneralPurpose（对抗性解耦专查）双审查，清单逐条判定后修复：

| # | 发现（审查方） | 判定 | 处置 |
|---|---|---|---|
| 1 | `'('*200` 触发 RecursionError 逃逸「不抛异常」契约（A，P1） | 实测复现，成立 | 按根因修：解析器加显式嵌套深度计数（≤50 层），不依赖 Python 栈余量；补 3 条回归测试 |
| 2 | 人工补跑非日对齐区间会使定时复盘顺延一天（A，P2） | 推演成立 | 按根因修：幂等判定先将 latest 对齐自然日 00:00；补非对齐回归测试 |
| 3 | calc 输出 E 记法但输入拒绝 `E`，结果无法代回（B，中） | 实测复现，成立 | tokenizer 支持科学计数法字面量，输入输出闭环；补 round-trip 测试 |
| 4 | schema 声称「精确不丢精度」对除法不成立（B，低） | 成立 | 措辞改为「28 位有效数字高精度计算」 |
| 5 | 3 处「每日/昨日/8 个工具」残留注释（B，低） | 成立 | review/__init__、routes_review、web types.ts 同步 |
| 6 | 复盘失败占用幂等记录，间隔放大后单次失败跨过整周期（B，中低） | 成立但为既有行为；自动重试会引入 LLM 故障时每分钟重发的更大风险 | 决策：维持不自动重试，config 注释写明权衡与手动补跑路径 |
| 7 | DST 时区下固定 86400 秒/天漂移（A P3 / B 低） | 成立但部署时区（UTC+8）无 DST | 注释显式声明假设，不改结构 |
| — | 审查 A 与 B 对递归深度结论矛盾（200 层击穿 vs 100 层安全） | 实测裁决：A 正确（每层括号 5 帧） | 见 #1 |

对抗审查放行项（B 附证据核实）：review→utils 依赖为 `__init__` 明文允许项、全包 0 次 import src/agent/*；所有涉改文件 ≤300 行、函数 ≤40 行、嵌套 ≤3 层；calc 双侧走统一 registry.execute 审计路径、零敞口；调度器只读 Settings；热写回链路（bootstrap 同实例注入）闭合。

## 测试

- 后端 `uv run pytest tests/ -q`：505 passed（新增 test_utils_calc.py 20 例；scheduler/config/server_review 扩 9 例）
- `ruff check` / `ruff format --check`：通过
- 前端 `npm run test`：214 passed（ReviewPanel 文案回归）
