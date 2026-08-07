"""金十源：官方 HTTP MCP（mcp.jin10.com），Bearer token 鉴权。

方法：日历（list_calendar）/ 快讯（list_flash 分页）/ 搜索（search_flash/search_news）/
文章详情（get_news）。字段解析宽松（.get 兜底），时间解析失败用当前时间兜底。
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

from src.research.providers.base import (
    CalendarEvent,
    FlashItem,
    ResearchSourceError,
    SOURCE_JIN10,
)
from src.research.providers.mcp_client import McpSession

# 金十/律动的时间串均为北京时间（UTC+8，无夏令时）：按固定时区解释，
# 与服务器本地时区解耦（M-TZ 修复：UTC 部署机上快讯窗口不再偏移 8h）
BEIJING_TZ = timezone(timedelta(hours=8))


def parse_ts(value: str) -> float:
    """时间串 → Unix 秒。支持 ISO 与 'YYYY-MM-DD HH:MM:SS'；失败返回当前时间。

    无时区的日期时间串按北京时间解释（数据源口径），带 %z 的 ISO 串尊重其自带时区。
    """
    if not value:
        return time.time()
    text = value.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            dt = datetime.strptime(text, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=BEIJING_TZ)
            return dt.timestamp()
        except ValueError:
            continue
    try:
        return float(text)
    except ValueError:
        return time.time()


def _flash_from_row(row: dict) -> FlashItem:
    """一条金十快讯行 → FlashItem（字段名以实测为准，.get 兜底）。

    title 兜底强制 str：快讯常缺 title，用 id 顶包时 id 可能是 JSON 数字，
    不转 str 会让聚合层 title[:40] 抛 TypeError（M3 修复）。
    """
    title = str(row.get("title") or row.get("id") or "")
    content = row.get("content") or ""
    return FlashItem(
        id=str(row.get("id", "")),
        source=SOURCE_JIN10,
        title=title,
        summary=content[:300],
        detail=content,
        url=row.get("url") or "",
        published_at=parse_ts(str(row.get("time") or row.get("create_time") or "")),
        raw=row,
    )


class Jin10Source:
    """金十数据源：持有 MCP 会话配置，方法级容错。"""

    def __init__(self, *, url: str, token: str, timeout: float = 60.0) -> None:
        self._url = url
        self._token = token
        self._timeout = timeout

    def _session(self) -> McpSession:
        return McpSession(kind="http", url=self._url, token=self._token, timeout=self._timeout)

    async def fetch_calendar(self) -> list[CalendarEvent]:
        """本周经济日历全量（今日+明日过滤由工具层做）。"""
        async with self._session() as s:
            text = await s.call_tool("list_calendar", {})
        rows = _safe_json_rows(text)
        events: list[CalendarEvent] = []
        for row in rows:
            try:
                star = int(row.get("star") or 0)
            except (TypeError, ValueError):
                star = 0
            events.append(
                CalendarEvent(
                    title=row.get("title", ""),
                    pub_time=row.get("pub_time", ""),
                    star=star,
                    actual=row.get("actual") or "",
                    consensus=row.get("consensus") or "",
                    previous=row.get("previous") or "",
                    affect_txt=row.get("affect_txt") or "",
                )
            )
        return events

    async def fetch_flash(self, hours: int = 24) -> list[FlashItem]:
        """近 hours 小时快讯：cursor 分页拉取（单会话复用），统一按时间窗口过滤。

        不依赖服务端排序假设：收集全部行后统一按 cutoff 过滤（防御倒序/乱序）；
        20 页硬上限兜底（超限时剩余数据由 24h 全量兜底覆盖）。
        """
        cutoff = time.time() - hours * 3600
        items: list[FlashItem] = []
        cursor = ""
        async with self._session() as s:  # 复用同一连接完成全部分页
            for _ in range(20):
                args = {"cursor": cursor} if cursor else {}
                text = await s.call_tool("list_flash", args)
                rows = _safe_json_rows(text)
                if not rows:
                    break
                new_cursor = _cursor_from(text)
                for row in rows:
                    items.append(_flash_from_row(row))
                if not new_cursor or new_cursor == cursor:
                    break
                cursor = new_cursor
        return [item for item in items if item.published_at >= cutoff]

    async def fetch_article_detail(self, item_id: str) -> str:
        """文章详情全文（get_news）。"""
        async with self._session() as s:
            text = await s.call_tool("get_news", {"id": item_id})
        return text

    async def search_news(self, keyword: str, limit: int = 20) -> list[FlashItem]:
        """关键词搜索：快讯 + 文章两个通道合并，按时间倒序取 limit 条。

        单通道失败降级用另一通道；双通道全挂抛 ResearchSourceError（M2 修复：
        聚合器据此判定源失败，不再把连接故障伪装成"未找到"）。
        """
        items: list[FlashItem] = []
        errors: list[str] = []
        for tool in ("search_flash", "search_news"):
            try:
                async with self._session() as s:
                    text = await s.call_tool(tool, {"keyword": keyword, "size": limit})
                rows = _safe_json_rows(text)
                items.extend(_flash_from_row(r) for r in rows)
            except ResearchSourceError as exc:
                errors.append(f"{tool}: {exc}")  # 单通道失败降级
        if len(errors) == 2:
            raise ResearchSourceError(f"金十搜索双通道均失败（{'；'.join(errors)}）")
        items.sort(key=lambda x: x.published_at, reverse=True)
        return items[:limit]


def _safe_json_rows(text: str) -> list[dict]:
    """从 MCP 返回文本中提取 data 列表；解析失败返回空（调用方降级）。"""
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return []
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    return []


def _cursor_from(text: str) -> str:
    """提取分页游标（list_flash 返回 next_cursor）。"""
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return ""
    if isinstance(payload, dict):
        return str(payload.get("next_cursor") or "")
    return ""
