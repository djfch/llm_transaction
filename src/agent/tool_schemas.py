"""LLM 工具的 JSON Schema 定义（中性格式，provider 各自转换为厂商格式）。

schema 只描述参数形状供 LLM 参考；真正的校验在执行函数内完成
（校验失败返回错误文本而非抛异常，见 tool_handlers）。
"""

from __future__ import annotations

from typing import Any

from src.market.intervals import GATE_CANDLE_INTERVALS

# Gate K 线 interval 合法枚举（单一数据源：src/market/intervals.py，覆盖 Gate 全周期）
_INTERVALS = list(GATE_CANDLE_INTERVALS)

SCHEMAS: dict[str, dict[str, Any]] = {
    "get_market_data": {
        "description": "获取合约行情：按时间升序逐根返回 K 线原始开盘/收盘/最高/最低/交易量，时间为北京时间（UTC+8）",
        "parameters": {
            "type": "object",
            "properties": {
                "contract": {"type": "string", "description": "合约名，如 BTC_USDT"},
                "interval": {
                    "type": "string",
                    "enum": _INTERVALS,
                    "description": "K 线周期，默认 1h",
                },
                "limit": {
                    "type": "integer",
                    "description": "取最近多少根 K 线（1-100），默认 24",
                },
            },
            "required": ["contract"],
        },
    },
    "get_indicators": {
        "description": (
            "获取合约全部技术指标当前值（EMA/MACD/RSI/KDJ/ROC/ATR/BOLL/量比/OBV/持仓量），"
            "逐行中文文本返回；指标按各自所需深度计算，与 get_market_data 的 limit 无关"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "contract": {"type": "string", "description": "合约名，如 BTC_USDT"},
                "interval": {
                    "type": "string",
                    "enum": _INTERVALS,
                    "description": "K 线周期，默认 1h",
                },
            },
            "required": ["contract"],
        },
    },
    "place_order": {
        "description": (
            "保证金下单、平仓或减仓。开仓/同向加仓提交 side、margin_usdt、leverage 和止损，"
            "代码换算名义仓位与 Gate 张数并固定逐仓；close=true 整仓市价平仓；"
            "reduce_pct 部分减仓。反手必须先平仓；下单前自动过硬风控"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "contract": {"type": "string", "description": "合约名，如 BTC_USDT"},
                "side": {
                    "type": "string",
                    "enum": ["long", "short"],
                    "description": "新增敞口方向；long=多，short=空",
                },
                "margin_usdt": {
                    "type": "number",
                    "description": "本单投入保证金，单位 USDT；代码按杠杆换算实际张数",
                },
                "price": {"type": "number", "description": "限价；不传为市价单"},
                "tif": {
                    "type": "string",
                    "enum": ["gtc", "ioc", "poc", "fok"],
                    "description": "限价单有效期策略，默认 gtc；市价单自动 ioc",
                },
                "reduce_pct": {
                    "type": "number",
                    "description": "部分减仓比例，必须在 0 与 1 之间；整仓平仓改用 close=true",
                },
                "close": {"type": "boolean", "description": "按市价平掉该合约全部持仓"},
                "leverage": {
                    "type": "integer",
                    "description": "新增敞口的杠杆倍数；保证金模式固定为逐仓",
                },
                "stop_loss_price": {
                    "type": "number",
                    "description": "止损触发价；开仓或同向加仓必填",
                },
                "take_profit_price": {"type": "number", "description": "止盈触发价，可选"},
            },
            "required": ["contract"],
        },
    },
    "update_tpsl": {
        "description": "更新当前整仓止盈止损：先创建完整的新保护单组，再取消该方向全部旧整仓保护单；止损必填，止盈可选",
        "parameters": {
            "type": "object",
            "properties": {
                "contract": {"type": "string", "description": "合约名，如 BTC_USDT"},
                "stop_loss_price": {"type": "number", "description": "新的止损触发价"},
                "take_profit_price": {"type": "number", "description": "新的止盈触发价，可选"},
            },
            "required": ["contract", "stop_loss_price"],
        },
    },
    "amend_order": {
        "description": "只修改未成交挂单价格；改变订单金额须撤单后重新提交 place_order",
        "parameters": {
            "type": "object",
            "properties": {
                "contract": {"type": "string", "description": "合约名"},
                "order_id": {"type": "string", "description": "订单 ID"},
                "price": {"type": "number", "description": "新价格"},
            },
            "required": ["contract", "order_id", "price"],
        },
    },
    "cancel_order": {
        "description": "撤销未成交挂单",
        "parameters": {
            "type": "object",
            "properties": {
                "contract": {"type": "string", "description": "合约名"},
                "order_id": {"type": "string", "description": "订单 ID"},
            },
            "required": ["contract", "order_id"],
        },
    },
    "set_price_alert": {
        "description": (
            "设置价格预警线：价格越线时触发唤醒；"
            "相同合约/方向/价格的预警线重复设置时直接返回已存在提示，不会重复创建；"
            "全局最多 10 条，达到上限须先用 cancel_price_alert 取消旧预警"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "contract": {"type": "string", "description": "合约名"},
                "direction": {
                    "type": "string",
                    "enum": ["above", "below"],
                    "description": "above=上穿触发，below=下穿触发",
                },
                "price": {"type": "number", "description": "触发价格"},
            },
            "required": ["contract", "direction", "price"],
        },
    },
    "cancel_price_alert": {
        "description": (
            "取消价格预警线：按合约/方向/价格精确匹配取消；不存在时返回未找到提示，不做任何修改"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "contract": {"type": "string", "description": "合约名"},
                "direction": {
                    "type": "string",
                    "enum": ["above", "below"],
                    "description": "above=上穿触发，below=下穿触发",
                },
                "price": {"type": "number", "description": "触发价格"},
            },
            "required": ["contract", "direction", "price"],
        },
    },
    "set_next_wakeup": {
        "description": (
            "设置下次唤醒时间（分钟）。行情关键期设短，平淡期设长；"
            "超出调度器上下限时自动钳制，返回实际生效值"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "minutes": {"type": "integer", "description": "多少分钟后唤醒"},
            },
            "required": ["minutes"],
        },
    },
    "write_note": {
        "description": "给未来的自己留笔记（跨轮传递判断要点，下轮上下文可见）",
        "parameters": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "笔记内容"},
            },
            "required": ["content"],
        },
    },
    "get_history": {
        "description": "查询近期成交记录与决策轮次历史",
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "条数上限（1-50），默认 20"},
            },
        },
    },
    "calc": {
        "description": (
            "计算数学表达式：支持 + - * / ^（幂）与括号，如 2*(3-1)^2 → 8。"
            "适合盈亏比等策略数字，禁止用来换算名义仓位或合约张数；28 位有效数字高精度计算"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "expression": {"type": "string", "description": "数学表达式，如 2*(3-1)^2"},
            },
            "required": ["expression"],
        },
    },
    "update_trade_plan": {
        "description": (
            "全文覆盖更新交易计划（全局唯一一份，不下单）：有明确的条件性交易意图时立案/修订，"
            "多合约想法写在同一份里；下轮唤醒会在上下文看到并核对。"
            "提交内容必须是计划完整新全文（覆盖旧版，不是增量补充）"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "计划完整新全文（Markdown，≤4000 字符）：入场条件/止损止盈/仓位思路等",
                },
            },
            "required": ["content"],
        },
    },
    "clear_trade_plan": {
        "description": "清空交易计划：计划已执行完毕或不再成立时调用，须写明原因（入审计）",
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "清空原因，如 '已按计划入场' / '行情破坏前提'",
                },
            },
            "required": ["reason"],
        },
    },
}
