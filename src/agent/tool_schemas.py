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
    "place_order": {
        "description": (
            "下单（开仓/平仓）。size 为张数：正=开多/买入，负=开空/卖出；"
            "会产生新敞口时必须提供 stop_loss_price 止损价，take_profit_price 可选；"
            "close=true 平掉该合约全部持仓；不传 price 为市价单。下单前自动过风控，拒绝时返回理由"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "contract": {"type": "string", "description": "合约名，如 BTC_USDT"},
                "size": {"type": "number", "description": "张数，正多负空；close=true 时忽略"},
                "price": {"type": "number", "description": "限价；不传为市价单"},
                "tif": {
                    "type": "string",
                    "enum": ["gtc", "ioc", "poc", "fok"],
                    "description": "限价单有效期策略，默认 gtc；市价单自动 ioc",
                },
                "reduce_only": {"type": "boolean", "description": "只减仓，默认 false"},
                "close": {"type": "boolean", "description": "平掉全部持仓，默认 false"},
                "leverage": {
                    "type": "integer",
                    "description": "本单使用的杠杆倍数（风控校验用），默认 1",
                },
                "margin_mode": {
                    "type": "string",
                    "enum": ["isolated", "cross"],
                    "description": "设置杠杆时的保证金模式，默认 isolated（逐仓）",
                },
                "stop_loss_price": {
                    "type": "number",
                    "description": "止损触发价；开仓、加仓或反手新开仓时必填",
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
        "description": "修改未成交挂单的价格或数量（至少提供一项）",
        "parameters": {
            "type": "object",
            "properties": {
                "contract": {"type": "string", "description": "合约名"},
                "order_id": {"type": "string", "description": "订单 ID"},
                "price": {"type": "number", "description": "新价格"},
                "size": {"type": "number", "description": "新张数（正多负空）"},
            },
            "required": ["contract", "order_id"],
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
            "设置价格预警线：价格越线时触发唤醒。预警线仅保存在内存，进程重启即失效"
            "（需重新设置）；相同合约/方向/价格的预警线重复设置时直接返回已存在提示，不会重复创建"
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
