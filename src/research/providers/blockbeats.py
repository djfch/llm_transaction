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
    """把律动接口的一条快讯记录转换为统一事实项并清理摘要中的 HTML。

    参数：
        row: dict，律动 MCP 返回的单条快讯字段

    返回：
        FlashItem，保留原始正文与来源字段的统一快讯对象
    """
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
    """移除快讯正文中的 HTML 标签并还原常见实体。

    参数：
        text: str，可能包含 HTML 片段的快讯正文

    返回：
        str，去除标签、替换常见实体并清理首尾空白的纯文本
    """
    import re

    text = re.sub(r"<[^>]+>", "", text)
    return text.replace("&nbsp;", " ").replace("&amp;", "&").strip()


class BlockbeatsSource:
    """律动数据源：stdio MCP 会话，方法级容错。"""

    def __init__(self, *, cmd: str, timeout: float = 60.0) -> None:
        """初始化律动数据源，记录 stdio MCP 启动命令与超时时间。

        参数：
            cmd: str，启动律动 MCP 子进程的命令串（如 npx -y blockbeats-mcp）
            timeout: float，单次 MCP 连接/调用的超时秒数，省略时默认 60 秒

        返回：
            None，仅初始化实例字段
        """
        self._cmd = cmd
        self._timeout = timeout

    def _session(self) -> McpSession:
        """新建一个律动 stdio MCP 会话（仅配置尚未连接，须以 async with 打开）。

        参数：无

        返回：
            McpSession：按本实例的命令与超时配置、并从环境变量
            BLOCKBEATS_API_KEY 取 API key 的 stdio 会话对象
        """
        return McpSession(
            kind="stdio",
            cmd=self._cmd,
            env_key="BLOCKBEATS_API_KEY",
            timeout=self._timeout,
        )

    async def fetch_flash(self, hours: int = 24) -> list[FlashItem]:
        """读取律动近 24 小时固定批次快讯并转换为统一事实项。

        参数：
            hours: int，调用方期望的小时窗口；当前上游接口固定返回 24 小时数据

        返回：
            list[FlashItem]，上游当前批次的全部有效快讯
        """
        async with self._session() as s:
            text = await s.call_tool("get_newsflash_24h", {})
        rows = _safe_rows(text)
        return [_flash_from_row(r) for r in rows]

    async def search_news(self, keyword: str, limit: int = 20) -> list[FlashItem]:
        """按关键词搜索律动快讯与文章，并按发布时间倒序截取结果。

        参数：
            keyword: str，新闻检索关键词
            limit: int，最多返回的结果条数

        返回：
            list[FlashItem]，按发布时间从新到旧排列的统一新闻项
        """
        async with self._session() as s:
            text = await s.call_tool(
                "search_news", {"keyword": keyword, "size": limit, "lang": "cn"}
            )
        rows = _safe_rows(text)
        items = [_flash_from_row(r) for r in rows]
        items.sort(key=lambda x: x.published_at, reverse=True)
        return items[:limit]

    async def fetch_indicators(self) -> str:
        """逐项调用律动宏观与加密指标工具并拼装中文快照，单项失败时就地标注。

        参数：无

        返回：
            str，按指标标题分段的市场快照文本
        """
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
    """从 MCP JSON 响应中安全提取字典类型的数据行。

    参数：
        text: str，MCP 工具返回的 JSON 文本

    返回：
        list[dict]，data 数组中的字典项；解析或结构失败时返回空列表
    """
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return []
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    return []


def _compact(text: str) -> str:
    """把 JSON 响应压缩为无缩进快照，并限制输出长度。

    参数：
        text: str，待压缩的 JSON 或普通响应文本

    返回：
        str，最长 800 字符的紧凑 JSON；无法解析时返回截断原文
    """
    try:
        payload = json.loads(text)
        return json.dumps(payload, ensure_ascii=False)[:800]
    except (json.JSONDecodeError, TypeError):
        return text[:800]
