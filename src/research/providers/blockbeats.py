"""律动源：官方 stdio MCP（npx blockbeats-mcp + BLOCKBEATS_API_KEY）。

方法：24h 快讯（get_newsflash_24h）/ 关键词搜索（search_news）/ 指标组快照
（ETF 流入/DXY/美债/M2/稳定币/情绪/OI/Bitfinex 多头——逐工具调用组装文本）。
字段解析宽松（.get 兜底）；单指标失败标注不可用，不拖垮整组。
"""

from __future__ import annotations

import json

from src.research.providers.base import (
    SOURCE_BLOCKBEATS,
    FlashItem,
    ResearchSourceError,
)
from src.research.providers.jin10 import parse_ts
from src.research.providers.mcp_client import McpSession

# 指标快照组：工具名 → （参数, 中文标签）
_INDICATOR_TOOLS: list[tuple[str, dict, str]] = [
    ("get_btc_etf_flow", {}, "BTC ETF 净流入"),
    ("get_dxy_index", {"timeframe": "1D"}, "美元指数 DXY"),
    ("get_us_treasury_yield", {"timeframe": "1D"}, "美债 10Y 收益率"),
    ("get_m2_supply", {"timeframe": "1Y"}, "全球 M2"),
    ("get_stablecoin_marketcap", {}, "稳定币市值"),
    ("get_sentiment_indicator", {}, "市场情绪指标"),
    ("get_contract_oi_data", {"dataType": "1D"}, "合约未平仓 OI"),
    ("get_bitfinex_long_positions", {"symbol": "btc", "timeframe": "1D"}, "Bitfinex 杠杆多头"),
]


def _flash_from_row(row: dict) -> FlashItem:
    """一条律动快讯行 → FlashItem（24h 快讯自带全文 content）。"""
    title = str(row.get("title") or "")  # 强制 str：防数字标题炸聚合层切片（同 M3）
    content = row.get("content") or ""
    return FlashItem(
        id=str(row.get("id", "")),
        source=SOURCE_BLOCKBEATS,
        title=title,
        summary=_strip_html(content)[:300],
        detail=content,
        url=row.get("link") or row.get("url") or "",
        published_at=parse_ts(str(row.get("create_time") or "")),
        raw=row,
    )


def _strip_html(text: str) -> str:
    """剥离 HTML 标签与实体（快讯正文为 HTML 片段）。"""
    import re

    text = re.sub(r"<[^>]+>", "", text)
    return text.replace("&nbsp;", " ").replace("&amp;", "&").strip()


class BlockbeatsSource:
    """律动数据源：stdio MCP 会话，方法级容错。"""

    def __init__(self, *, cmd: str, timeout: float = 60.0) -> None:
        self._cmd = cmd
        self._timeout = timeout

    def _session(self) -> McpSession:
        return McpSession(
            kind="stdio",
            cmd=self._cmd,
            env_key="BLOCKBEATS_API_KEY",
            timeout=self._timeout,
        )

    async def fetch_flash(self, hours: int = 24) -> list[FlashItem]:
        """24h 全量快讯（该接口固定 50 条，hours 参数仅语义化保留）。"""
        async with self._session() as s:
            text = await s.call_tool("get_newsflash_24h", {})
        rows = _safe_rows(text)
        return [_flash_from_row(r) for r in rows]

    async def search_news(self, keyword: str, limit: int = 20) -> list[FlashItem]:
        """关键词搜索（快讯/文章混合通道）。"""
        async with self._session() as s:
            text = await s.call_tool(
                "search_news", {"keyword": keyword, "size": limit, "lang": "cn"}
            )
        rows = _safe_rows(text)
        items = [_flash_from_row(r) for r in rows]
        items.sort(key=lambda x: x.published_at, reverse=True)
        return items[:limit]

    async def fetch_indicators(self) -> str:
        """指标组快照文本：逐工具调用，单失败标注不可用。"""
        lines: list[str] = []
        async with self._session() as s:
            for tool, args, label in _INDICATOR_TOOLS:
                try:
                    text = await s.call_tool(tool, args)
                    lines.append(f"## {label}\n{_compact(text)}")
                except ResearchSourceError as exc:
                    lines.append(f"## {label}\n（不可用：{exc}）")
        return "\n\n".join(lines)


def _safe_rows(text: str) -> list[dict]:
    """提取 data 列表；解析失败返回空（调用方降级）。"""
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return []
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    return []


def _compact(text: str) -> str:
    """压缩 JSON 文本为可读快照（去掉换行缩进，截断超长）。"""
    try:
        payload = json.loads(text)
        return json.dumps(payload, ensure_ascii=False)[:800]
    except (json.JSONDecodeError, TypeError):
        return text[:800]
