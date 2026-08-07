"""研报工具的 JSON Schema 定义（中性格式，provider 各自转换为厂商格式）。

9 个只读工具 + 1 个写工具（submit_causal_links 为唯一写出口），无任何交易工具。
schema 只描述参数形状供 LLM 参考；真正的校验在执行函数内完成
（校验失败返回错误文本而非抛异常，见 tool_handlers）。
"""

from __future__ import annotations

from typing import Any

SCHEMAS: dict[str, dict[str, Any]] = {
    "fetch_calendar": {
        "description": "获取经济日历今日+明日高星事件（star≥3）：事件名、公布时间、实际/预期/前值、影响方向",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    "fetch_flash": {
        "description": "获取近 hours 小时全量快讯（金十+律动合并，时间+标题+摘要紧凑格式），已注入的信息不要重复拉取",
        "parameters": {
            "type": "object",
            "properties": {
                "hours": {"type": "integer", "description": "回溯小时数（1-48），默认 24"},
            },
            "required": [],
        },
    },
    "fetch_indicators": {
        "description": "获取硬数据指标快照：BTC ETF 净流入、美元指数 DXY、美债 10Y 收益率、M2、稳定币市值、市场情绪、合约 OI、Bitfinex 杠杆多头",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    "get_macro_series": {
        "description": "获取 FRED 宏观历史序列（趋势判断用）：最新值+窗口变化+最近观测表格。别名：cpi/pce/fed_funds_rate/10y_treasury/m2/dollar_index/vix/unemployment/nonfarm_payrolls 等，或直接传 FRED series ID",
        "parameters": {
            "type": "object",
            "properties": {
                "indicator": {
                    "type": "string",
                    "description": "指标别名或 FRED series ID，如 cpi / DGS10",
                },
                "look_back": {"type": "integer", "description": "回溯天数（30-1825），默认 365"},
            },
            "required": ["indicator"],
        },
    },
    "get_prediction_markets": {
        "description": "获取 Polymarket 预测市场隐含概率（事件定价，比新闻更前置）：如 Fed rate cut / recession 2026 / 地缘事件",
        "parameters": {
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "事件关键词，如 Fed rate cut"},
                "limit": {"type": "integer", "description": "返回条数（1-10），默认 6"},
            },
            "required": ["topic"],
        },
    },
    "fetch_article_detail": {
        "description": "按 id 获取单条快讯/文章正文全文（预注入只有标题+摘要，细读时用）",
        "parameters": {
            "type": "object",
            "properties": {"id": {"type": "string", "description": "快讯/文章 id"}},
            "required": ["id"],
        },
    },
    "search_news": {
        "description": "按关键词检索历史快讯/文章（金十+律动+本地事实层合并去重），深挖事件来龙去脉",
        "parameters": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "搜索关键词，如 非农 / 美联储 / 油价"},
                "limit": {"type": "integer", "description": "条数上限（1-30），默认 20"},
            },
            "required": ["keyword"],
        },
    },
    "read_timeline": {
        "description": "读取事件时间线（事实层，近 N 天客观记录：时间+事件+来源），看跨天因果链",
        "parameters": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "回溯天数（1-30），默认 7"},
                "limit": {"type": "integer", "description": "条数上限（1-500），默认 200"},
            },
            "required": [],
        },
    },
    "read_judgments": {
        "description": "读取历史研报结论（判断层，近 N 天）：方向/置信度/依据/验证结果/错因——自我纠错必读",
        "parameters": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "回溯天数（1-30），默认 7"},
            },
            "required": [],
        },
    },
    "submit_causal_links": {
        "description": (
            "提交本次分析得出的链式因果链（唯一写工具）：chain 为有序节点数组"
            "（事件→推断→市场反应→标的结论），节点可带 kind 与 timeline_id 引用事实层；"
            "整链带 confidence(0-1)、evidence 依据。无需传研报 id——代码在本轮研报"
            "落库后自动回填关联。提交真实推导过的链，不要凑数"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "chain": {
                    "type": "array",
                    "description": "有序节点链（2-6 个节点）",
                    "items": {
                        "type": "object",
                        "properties": {
                            "node": {
                                "type": "string",
                                "description": "节点内容（事件/推断/反应/结论）",
                            },
                            "kind": {
                                "type": "string",
                                "description": "节点类型：事件/推断/市场反应/标的结论",
                            },
                            "timeline_id": {
                                "type": "integer",
                                "description": "事件节点引用事实层 id，可空",
                            },
                        },
                        "required": ["node"],
                    },
                },
                "confidence": {"type": "number", "description": "整链置信度 0-1"},
                "evidence": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "依据列表（可溯源）",
                },
            },
            "required": ["chain", "confidence"],
        },
    },
}
