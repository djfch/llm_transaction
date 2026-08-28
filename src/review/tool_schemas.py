"""复盘工具的 JSON Schema 定义（中性格式，provider 各自转换为厂商格式）。

14 个只读工具 + 4 个写工具（submit_strategy_revision / submit_indicator_config /
submit_research_review / submit_research_prompt_revision 为写出口），无任何交易工具。
schema 只描述参数形状供 LLM 参考；真正的校验在执行函数内完成
（校验失败返回错误文本而非抛异常，见 tool_handlers / tool_indicators / tool_research）。
"""

from __future__ import annotations

from typing import Any

from src.market.intervals import GATE_CANDLE_INTERVALS
from src.review.tool_indicators import indicator_menu_items

# 可选指标菜单（模块 import 时由指标注册表动态生成，与 get_indicator_config 输出同源）
_INDICATOR_MENU = "；".join(indicator_menu_items())

_START_END_PROPS = {
    "start_ts": {"type": "number", "description": "统计起点（Unix 秒，含）"},
    "end_ts": {"type": "number", "description": "统计终点（Unix 秒，不含）"},
}

SCHEMAS: dict[str, dict[str, Any]] = {
    "get_review_stats": {
        "description": (
            "获取复盘区间的平仓统计（代码计算，口径固定）：平仓笔数、总盈亏、胜率、"
            "盈亏比、平均盈/亏、最大单笔亏损、各合约分布"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                **_START_END_PROPS,
                "strategy_md5": {
                    "type": "string",
                    "description": "按策略版本过滤（策略书原文 md5），可空",
                },
                "contract": {"type": "string", "description": "按合约过滤，如 BTC_USDT，可空"},
            },
            "required": ["start_ts", "end_ts"],
        },
    },
    "list_decision_rounds": {
        "description": (
            "列出复盘区间内的决策轮次：round_id、wake_source、strategy_md5、"
            "一行摘要、error、时间，按时间倒序"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                **_START_END_PROPS,
                "strategy_md5": {
                    "type": "string",
                    "description": "按策略版本过滤（策略书原文 md5），可空",
                },
                "limit": {"type": "integer", "description": "条数上限（1-100），默认 20"},
            },
            "required": ["start_ts", "end_ts"],
        },
    },
    "get_decision_detail": {
        "description": "查看单轮决策详情：决策摘要 + LLM 原始输出（截断）+ wake_source + strategy_md5",
        "parameters": {
            "type": "object",
            "properties": {
                "round_id": {"type": "string", "description": "决策轮次 ID（完整 round_id）"},
                "max_chars": {
                    "type": "integer",
                    "description": "llm_raw 最大返回字符数（1-20000），默认 4000",
                },
            },
            "required": ["round_id"],
        },
    },
    "get_tool_call_chain": {
        "description": (
            "还原一轮决策的工具调用链：按 seq 排序的工具名、参数、风控判定、"
            "结果摘要（每条截断 500 字符）与耗时"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "round_id": {"type": "string", "description": "决策轮次 ID（完整 round_id）"},
            },
            "required": ["round_id"],
        },
    },
    "list_trades": {
        "description": "查询复盘区间内的成交明细：时间、合约、size、price、fee、pnl、source、round_id",
        "parameters": {
            "type": "object",
            "properties": {
                **_START_END_PROPS,
                "contract": {"type": "string", "description": "按合约过滤，如 BTC_USDT，可空"},
                "source": {
                    "type": "string",
                    "description": (
                        "按来源过滤（llm_open/llm_close/tpsl_close/user_close/liquidation），可空"
                    ),
                },
                "limit": {"type": "integer", "description": "条数上限（1-200），默认 50"},
            },
            "required": ["start_ts", "end_ts"],
        },
    },
    "get_round_context": {
        "description": "查看一轮决策的上下文快照（audit_rounds.context_snapshot，截断返回）",
        "parameters": {
            "type": "object",
            "properties": {
                "round_id": {"type": "string", "description": "决策轮次 ID（完整 round_id）"},
                "max_chars": {
                    "type": "integer",
                    "description": "快照最大返回字符数（1-20000），默认 4000",
                },
            },
            "required": ["round_id"],
        },
    },
    "get_strategy_versions": {
        "description": (
            "查看策略书版本：不传 version_id 返回版本列表（最多 50 条）+ 当前策略全文；"
            "传 version_id 返回该版本完整原文"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "version_id": {"type": "integer", "description": "版本号（vN 的 N），可空"},
            },
        },
    },
    "calc": {
        "description": (
            "计算数学表达式：支持 + - * / ^（幂）与括号，如 2*(3-1)^2 → 8。"
            "适合盈亏比、回撤幅度等衍生计算，28 位有效数字高精度计算"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "expression": {"type": "string", "description": "数学表达式，如 2*(3-1)^2"},
            },
            "required": ["expression"],
        },
    },
    "submit_strategy_revision": {
        "description": (
            "提交策略书修订（策略书写出口）：new_prompt_md 为策略书完整新文本（全文重写）。"
            "服务端校验（≥100 字符、≤32KB、与当前版本有差异），通过则生成新版本 vN 并于"
            "下一轮决策生效；拒绝则返回原因列表，修正后可重试。无实质收获时不要调用"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "new_prompt_md": {
                    "type": "string",
                    "description": "策略书完整新文本（Markdown，全文重写，不是 diff）",
                },
                "reason": {"type": "string", "description": "修订理由（引用复盘证据）"},
            },
            "required": ["new_prompt_md", "reason"],
        },
    },
    "get_research_prompt_versions": {
        "description": (
            "查看研报提示词版本：不传 version_id 返回版本列表（最多 50 条）+ 当前提示词全文；"
            "传 version_id 返回该版本完整原文"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "version_id": {"type": "integer", "description": "版本号（vN 的 N），可空"},
            },
        },
    },
    "submit_research_prompt_revision": {
        "description": (
            "提交研报提示词修订（研报提示词写出口）：new_prompt_md 为提示词完整新文本"
            "（全文重写）。服务端校验（≥100 字符、≤32KB、与当前版本有差异），通过则生成"
            "草稿版本 vN，本轮复盘报告提交成功后统一生效；拒绝则返回原因列表，修正后可重试。"
            "仅当研报复盘发现可重复偏差且有明确改进方向时才调用，无实质收获时不要调用"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "new_prompt_md": {
                    "type": "string",
                    "description": "研报提示词完整新文本（Markdown，全文重写，不是 diff）",
                },
                "reason": {"type": "string", "description": "修订理由（引用研报复盘证据）"},
            },
            "required": ["new_prompt_md", "reason"],
        },
    },
    "get_indicators": {
        "description": (
            "查看指定合约的当前技术指标面板（全部注册指标逐行列出当前值，数据不足显示"
            " 无数据）；合约必须在 watchlist 内"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "contract": {"type": "string", "description": "合约代码，如 BTC_USDT"},
                "interval": {
                    "type": "string",
                    "enum": list(GATE_CANDLE_INTERVALS),
                    "description": "K 线周期，默认 1h",
                },
            },
            "required": ["contract"],
        },
    },
    "get_indicator_config": {
        "description": (
            "查看当前生效的指标短名单（执行 agent 每轮注入上下文的技术指标键）与"
            "可选指标全集菜单（key=名称/分组）"
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    "submit_indicator_config": {
        "description": (
            "提交指标短名单改写（全文替换，不是增量叠加）：shortlist 为完整新名单"
            f"（去重后 1~8 个；可选键：{_INDICATOR_MENU}）。服务端校验（未知键/数量越界/"
            "与当前无差异/reason 为空），通过则生成新版本 vN 并于下一轮决策生效；拒绝则"
            "返回原因列表，修正后可重试。无实质复盘依据不要调用"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "shortlist": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": '指标键完整新列表（1~8 个），如 ["ema20", "rsi14", "macd"]',
                },
                "reason": {
                    "type": "string",
                    "description": "改写依据（引用具体成交/轮次等复盘证据）",
                },
            },
            "required": ["shortlist", "reason"],
        },
    },
    "list_research_review_candidates": {
        "description": (
            "列出已到期、尚未复盘且客观行情数据可批改的研报逐标的结论候选"
            "（按到期时刻升序，数据不可用者自动跳过并计数）："
            "report_id、contract、方向、置信度、horizon、研报时间与到期时刻"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "条数上限（1-100），默认 20"},
            },
        },
    },
    "get_research_review_case": {
        "description": (
            "读取单个研报复盘案例的完整材料：逐标的结论原文与逐条依据、当时市场快照、"
            "研报轮上下文快照、代码归一化记录（policy_adjustments）、当时提交的因果链"
            "（只读）、代码按历史 K 线计算的客观行情结果（仅供参考）。提交批改"
            "（submit_research_review）前必须先读案例；读案例后可用 read_timeline 与"
            " get_macro_series 回看案例窗口内的事实层与宏观序列"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "report_id": {"type": "integer", "description": "研报编号（候选列表中的研报#N）"},
                "contract": {"type": "string", "description": "合约代码，如 BTC_USDT"},
            },
            "required": ["report_id", "contract"],
        },
    },
    "list_research_reviews": {
        "description": (
            "查询历史研报复盘记录（完整评价五段+客观结果摘要），可按时间窗/合约过滤；"
            "修订研报提示词前用它核对同类问题是否重复出现"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "start_ts": {"type": "number", "description": "时间窗起点（Unix 秒，含），可空"},
                "end_ts": {"type": "number", "description": "时间窗终点（Unix 秒，不含），可空"},
                "contract": {"type": "string", "description": "按合约过滤，如 BTC_USDT，可空"},
                "limit": {"type": "integer", "description": "条数上限（1-100），默认 20"},
            },
        },
    },
    "submit_research_review": {
        "description": (
            "提交对单个研报逐标的结论的复盘批改（研报复盘写出口）：方向关系/推理质量/"
            "置信度合规三个枚举评价（各配独立理由文本）+ 改进建议 + evidence_reviews "
            "逐条依据评价（与原研报依据一一对应，evidence_index 不重不漏覆盖 0..N-1，"
            "每条含事实核对与推理支撑双枚举及写明核对来源的说明）。客观行情结果由代码附加，"
            "不得提交 outcome 字段。须先用 get_research_review_case 读取案例；通过则暂存草稿，"
            "随本轮复盘报告落库统一生效"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "report_id": {"type": "integer", "description": "研报编号"},
                "contract": {"type": "string", "description": "合约代码，如 BTC_USDT"},
                "direction_relation": {
                    "type": "string",
                    "enum": ["realized", "diverged", "digested", "invalidated", "unverifiable"],
                    "description": (
                        "方向关系枚举：realized=兑现（方向与走势一致）、diverged=背离、"
                        "digested=震荡消化（区间内未兑现也未破坏）、invalidated=失效"
                        "（走势反向破坏结论前提）、unverifiable=无法核对"
                    ),
                },
                "direction_reason": {
                    "type": "string",
                    "description": "方向关系评价理由（结合客观行情结果说明判定依据）",
                },
                "reasoning_quality": {
                    "type": "string",
                    "enum": ["sound", "partial", "flawed", "unreviewable"],
                    "description": (
                        "推理质量枚举（只评价当时推理方法，不评价因果链内容正确性）："
                        "sound=推理基本成立、partial=推理部分成立、flawed=推理存在明显问题、"
                        "unreviewable=无法评价（客观数据不足等原因无法复盘时用于结案）"
                    ),
                },
                "reasoning_review": {
                    "type": "string",
                    "description": "推理质量评价复核文本（指出推理方法上的具体得失）",
                },
                "evidence_reviews": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "evidence_index": {
                                "type": "integer",
                                "description": "原研报依据序号（从 0 开始）",
                            },
                            "fact_status": {
                                "type": "string",
                                "enum": ["confirmed", "contradicted", "unverifiable"],
                                "description": (
                                    "事实核对枚举：confirmed=已证实、contradicted=已证伪、"
                                    "unverifiable=无法核实"
                                ),
                            },
                            "reasoning_status": {
                                "type": "string",
                                "enum": [
                                    "supported",
                                    "partially_supported",
                                    "unsupported",
                                    "counterevidence",
                                    "unverifiable",
                                ],
                                "description": (
                                    "推理支撑枚举：supported=支撑结论、"
                                    "partially_supported=部分支撑、unsupported=不支撑、"
                                    "counterevidence=构成反证、unverifiable=无法核实"
                                ),
                            },
                            "explanation": {
                                "type": "string",
                                "description": "评价说明（必须写明核对来源，如某工具结果或案例材料）",
                            },
                        },
                        "required": [
                            "evidence_index",
                            "fact_status",
                            "reasoning_status",
                            "explanation",
                        ],
                    },
                    "description": "逐条依据评价列表，数量与 evidence_index 必须与案例材料的依据一一对应",
                },
                "confidence_assessment": {
                    "type": "string",
                    "enum": ["appropriate", "too_high", "too_low", "unreviewable"],
                    "description": (
                        "置信度合规枚举：appropriate=与证据强度匹配、too_high=偏高、"
                        "too_low=偏低、unreviewable=无法评价"
                    ),
                },
                "confidence_reason": {
                    "type": "string",
                    "description": "置信度合规评价理由（引用证据强度或归一化记录）",
                },
                "improvement_advice": {
                    "type": "string",
                    "description": "改进建议（下一轮研报应如何改进；无实质建议时说明理由）",
                },
            },
            "required": [
                "report_id",
                "contract",
                "direction_relation",
                "direction_reason",
                "reasoning_quality",
                "reasoning_review",
                "evidence_reviews",
                "confidence_assessment",
                "confidence_reason",
                "improvement_advice",
            ],
        },
    },
    "read_timeline": {
        "description": (
            "回看已读案例窗口内的事实层记录（金十/律动快讯、日历、指标），供逐条依据的"
            "事实核对引用。仅限窗口 [案例创建时间, min(窗口终点, 当前时间)] 内："
            "越出窗口即拒绝（防止用案例之后的信息指责当时判断）。"
            "须先用 get_research_review_case 读取案例"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "report_id": {"type": "integer", "description": "已读案例的研报编号"},
                "contract": {"type": "string", "description": "已读案例的合约代码"},
                "start_ts": {"type": "number", "description": "回看起点（Unix 秒，含）"},
                "end_ts": {"type": "number", "description": "回看终点（Unix 秒，不含）"},
                "kind": {
                    "type": "string",
                    "description": "记录类型过滤（flash/calendar/indicator），可空",
                },
                "keyword": {"type": "string", "description": "标题子串过滤，可空"},
                "limit": {"type": "integer", "description": "条数上限（1-200），默认 50"},
            },
            "required": ["report_id", "contract", "start_ts", "end_ts"],
        },
    },
    "get_macro_series": {
        "description": (
            "回看已读案例窗口内的 FRED 宏观序列（如 cpi/10y_treasury/m2），供宏观依据的"
            "事实核对引用。end_ts 缺省为案例窗口终点（与当前时间的较小者），不得晚于它；"
            "序列起点不早于案例创建时间（窗口纪律同 read_timeline）。"
            "须先用 get_research_review_case 读取案例"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "report_id": {"type": "integer", "description": "已读案例的研报编号"},
                "contract": {"type": "string", "description": "已读案例的合约代码"},
                "indicator": {
                    "type": "string",
                    "description": "宏观指标代码（如 cpi / 10y_treasury / m2）",
                },
                "end_ts": {
                    "type": "number",
                    "description": "序列终点（Unix 秒），可空，缺省为案例窗口终点",
                },
            },
            "required": ["report_id", "contract", "indicator"],
        },
    },
}
