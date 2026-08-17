"""东京、伦敦、纽约官方交易日页面解析与本地年度缓存。"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable, Iterator
from datetime import date, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import httpx

from src.audit.logger import get_logger

logger = get_logger(__name__)

MARKET_URLS = {
    "XTKS": "https://www.jpx.co.jp/english/corporate/about-jpx/calendar/",
    "XLON": (
        "https://api.londonstockexchange.com/api/v1/pages?path=equities-trading/business-days"
    ),
    "XNYS": "https://www.nyse.com/trade/hours-calendars",
}
_PARSERS: dict[str, Callable[[str], dict[int, set[str]]]] = {}
CalendarFetcher = Callable[[str, str], Awaitable[str]]


class _TableParser(HTMLParser):
    """把 HTML 表格压平成单元格文本行。"""

    def __init__(self) -> None:
        """初始化空行、当前行和当前单元格缓冲。

        参数：无

        返回：
            None：就地初始化解析状态
        """
        super().__init__()
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, _attrs: list[tuple[str, str | None]]) -> None:
        """进入表格行或单元格时创建文本缓冲。

        参数：
            tag: str，HTML 标签名
            _attrs: list[tuple[str, str | None]]，标签属性（本解析器不使用）

        返回：
            None：就地更新当前行或单元格
        """
        if tag == "tr":
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = []

    def handle_data(self, data: str) -> None:
        """收集当前单元格内的文本片段。

        参数：
            data: str，HTML 文本片段

        返回：
            None：就地追加单元格文本
        """
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        """结束单元格或表格行时提交标准化文本。

        参数：
            tag: str，HTML 标签名

        返回：
            None：就地提交单元格或行
        """
        if tag in {"td", "th"} and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None


def _rows(html: str) -> list[list[str]]:
    """解析 HTML 中全部表格行。

    参数：
        html: str，含表格的 HTML

    返回：
        list[list[str]]：逐行单元格文本
    """
    parser = _TableParser()
    parser.feed(html)
    return parser.rows


def _require(result: dict[int, set[str]], source: str) -> dict[int, set[str]]:
    """拒绝没有任何年度休市日的伪成功解析结果。

    参数：
        result: dict[int, set[str]]，解析出的年度休市日
        source: str，来源名

    返回：
        dict[int, set[str]]：非空原结果

    异常：
        ValueError，没有有效年度数据时抛出
    """
    if not result or not any(result.values()):
        raise ValueError(f"{source} 页面没有解析出年度休市日")
    return result


def parse_jpx(content: str) -> dict[int, set[str]]:
    """解析 JPX 年度市场休市日页面。

    参数：
        content: str，JPX 官方 HTML

    返回：
        dict[int, set[str]]：年份到 ISO 日期集合
    """
    headings = list(re.finditer(r"<h2[^>]*>\s*<span>(20\d{2})</span>\s*</h2>", content))
    result: dict[int, set[str]] = {}
    for index, match in enumerate(headings):
        year = int(match.group(1))
        end = headings[index + 1].start() if index + 1 < len(headings) else len(content)
        for row in _rows(content[match.end() : end]):
            raw = row[0].split("(", 1)[0].replace(".", "").strip()
            try:
                parsed = datetime.strptime(f"{raw} {year}", "%b %d %Y").date()
            except (ValueError, IndexError):
                continue
            result.setdefault(year, set()).add(parsed.isoformat())
    return _require(result, "JPX")


def _strings(value: Any) -> Iterator[str]:
    """递归遍历 JSON 结构中的字符串。

    参数：
        value: Any，任意 JSON 值

    返回：
        Iterator[str]：深度遍历得到的字符串
    """
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def parse_lse(content: str) -> dict[int, set[str]]:
    """解析 LSE 页面 API 返回的营业日 JSON。

    参数：
        content: str，LSE 官方页面 JSON

    返回：
        dict[int, set[str]]：年份到完整休市日集合（半日市不计入）
    """
    payload = json.loads(content)
    result: dict[int, set[str]] = {}
    for html in _strings(payload):
        if "NON-trading day" not in html:
            continue
        for row in _rows(html):
            if not any("NON-trading day" in cell for cell in row):
                continue
            try:
                parsed = datetime.strptime(row[0].replace("\xa0", " "), "%A %d %B %Y").date()
            except (ValueError, IndexError):
                continue
            result.setdefault(parsed.year, set()).add(parsed.isoformat())
    return _require(result, "LSE")


def parse_nyse(content: str) -> dict[int, set[str]]:
    """解析 NYSE 年度节假日表格。

    参数：
        content: str，NYSE 官方 HTML

    返回：
        dict[int, set[str]]：年份到完整休市日集合
    """
    result: dict[int, set[str]] = {}
    for rows in [_rows(content)]:
        header = next((row for row in rows if row and row[0] == "Holiday"), None)
        if header is None:
            continue
        years = [int(value) for value in header[1:] if value.isdigit()]
        start = rows.index(header) + 1
        for row in rows[start:]:
            for year, raw in zip(years, row[1:], strict=False):
                match = re.search(r"[A-Za-z]+,\s+([A-Za-z]+)\s+(\d{1,2})", raw)
                if match is None:
                    continue
                parsed = datetime.strptime(
                    f"{match.group(1)} {match.group(2)} {year}", "%B %d %Y"
                ).date()
                result.setdefault(year, set()).add(parsed.isoformat())
    return _require(result, "NYSE")


_PARSERS.update({"XTKS": parse_jpx, "XLON": parse_lse, "XNYS": parse_nyse})


class MarketCalendarProvider:
    """刷新三家官方休市日并提供带缓存、可降级的交易日判断。"""

    def __init__(self, cache_path: Path, fetcher: CalendarFetcher | None = None) -> None:
        """初始化缓存路径和可注入 HTTP 边界，并读取已有缓存。

        参数：
            cache_path: Path，JSON 缓存路径
            fetcher: CalendarFetcher | None，可注入的异步页面读取函数

        返回：
            None：就地初始化日历状态
        """
        self._path = cache_path
        self._fetcher = fetcher or self._fetch
        self._holidays: dict[str, dict[int, set[str]]] = {}
        self._last_refreshed_at: float | None = None
        self._errors: dict[str, str] = {}
        self._live = False
        self._fallback_used = False
        self._load()

    async def _fetch(self, _market: str, url: str) -> str:
        """从官方地址读取页面文本。

        参数：
            _market: str，市场代码（默认 HTTP 实现不使用）
            url: str，官方页面地址

        返回：
            str：响应正文

        异常：
            httpx.HTTPError，请求失败或非成功状态时抛出
        """
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.text

    async def refresh(self) -> None:
        """并行刷新三家官方页面，单源失败保留原缓存。

        参数：无

        返回：
            None：更新内存与磁盘缓存，并记录逐来源错误
        """

        async def one(market: str) -> tuple[str, dict[int, set[str]] | None, str]:
            """读取并解析单个市场来源。

            参数：
                market: str，市场代码

            返回：
                tuple[str, dict[int, set[str]] | None, str]：市场、结果和错误
            """
            try:
                content = await self._fetcher(market, MARKET_URLS[market])
                return market, _PARSERS[market](content), ""
            except Exception as exc:
                return market, None, str(exc)

        results = await asyncio.gather(*(one(market) for market in MARKET_URLS))
        self._errors = {}
        any_success = False
        for market, parsed, error in results:
            if parsed is None:
                self._errors[market] = error
                logger.warning("%s 官方交易日刷新失败：%s", market, error)
                continue
            self._holidays[market] = parsed
            any_success = True
        self._live = any_success and not self._errors
        if self._live:
            self._fallback_used = False
        if any_success:
            self._last_refreshed_at = time.time()
            self._save()

    def is_trading_day(self, market: str, target: date) -> bool:
        """判断目标日期是否为市场交易日；未知工作日按约定降级为开市。

        参数：
            market: str，XTKS/XLON/XNYS
            target: date，市场会话标签日期

        返回：
            bool：周末或已知休市日为 False，其余为 True
        """
        if target.weekday() >= 5:
            return False
        holidays = self._holidays.get(market, {}).get(target.year)
        if holidays is None:
            self._fallback_used = True
            return True
        return target.isoformat() not in holidays

    def status(self) -> dict[str, Any]:
        """返回前端可展示的日历刷新状态。

        参数：无

        返回：
            dict[str, Any]：state、最近刷新时间、逐来源错误与警告
        """
        no_cache = not any(self._holidays.values())
        state = "error" if no_cache and self._errors else "fallback"
        if self._live and not self._fallback_used and not self._errors:
            state = "ok"
        return {
            "state": state,
            "last_refreshed_at": self._last_refreshed_at,
            "errors": dict(self._errors),
            "warning": "官方日历不可确认的工作日按交易日执行" if state != "ok" else "",
        }

    def _load(self) -> None:
        """从磁盘恢复年度缓存，损坏文件降级为空缓存。

        参数：无

        返回：
            None：就地恢复缓存状态
        """
        if not self._path.exists():
            return
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
            self._last_refreshed_at = payload.get("last_refreshed_at")
            self._holidays = {
                market: {int(year): set(days) for year, days in years.items()}
                for market, years in payload.get("holidays", {}).items()
            }
        except (OSError, ValueError, TypeError):
            logger.warning("官方交易日日历缓存损坏，已降级为空缓存", exc_info=True)

    def _save(self) -> None:
        """把当前年度休市日原子写入被 Git 忽略的缓存文件。

        参数：无

        返回：
            None：创建目录并替换缓存文件
        """
        payload = {
            "last_refreshed_at": self._last_refreshed_at,
            "holidays": {
                market: {str(year): sorted(days) for year, days in years.items()}
                for market, years in self._holidays.items()
            },
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self._path)
