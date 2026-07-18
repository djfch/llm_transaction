"""LLM 工具的 JSON Schema 定义（中性格式，provider 各自转换为厂商格式）。

schema 只描述参数形状供 LLM 参考；真正的校验在执行函数内完成
（校验失败返回错误文本而非抛异常，见 tool_handlers）。
"""

from __future__ import annotations

from typing import Any

# Gate K 线 interval 合法枚举（实现计划附录，已核实）
_INTERVALS = ["10s", "1m", "5m", "15m", "30m", "1h", "4h", "8h", "1d", "7d"]

SCHEMAS: dict[str, dict[str, Any]] = {
    "get_market_data": {
        "description": "获取合约行情：近期 K 线摘要（open/close/高低/变化率）、标记价与资金费率",
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
    "get_account": {
        "description": "获取账户状态：权益估值、可用余额、未实现盈亏与全部持仓明细",
        "parameters": {"type": "object", "properties": {}},
    },
    "place_order": {
        "description": (
            "下单（开仓/平仓）。size 为张数：正=开多/买入，负=开空/卖出；"
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
            },
            "required": ["contract"],
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
    "set_leverage": {
        "description": "设置合约杠杆倍数（受风控 max_leverage 上限约束，超限拒绝）",
        "parameters": {
            "type": "object",
            "properties": {
                "contract": {"type": "string", "description": "合约名"},
                "leverage": {"type": "integer", "description": "杠杆倍数"},
                "margin_mode": {
                    "type": "string",
                    "enum": ["isolated", "cross"],
                    "description": "保证金模式，默认 isolated（逐仓）",
                },
            },
            "required": ["contract", "leverage"],
        },
    },
    "set_price_alert": {
        "description": "设置价格预警线：价格越线时触发唤醒",
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
}
