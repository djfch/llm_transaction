"""复盘工具的 JSON Schema 定义（中性格式，provider 各自转换为厂商格式）。

7 个只读工具 + 1 个写工具（submit_strategy_revision 为唯一写出口），无任何交易工具。
schema 只描述参数形状供 LLM 参考；真正的校验在执行函数内完成
（校验失败返回错误文本而非抛异常，见 tool_handlers）。
"""

from __future__ import annotations

from typing import Any

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
            "提交策略书修订（唯一写出口）：new_prompt_md 为策略书完整新文本（全文重写）。"
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
}
