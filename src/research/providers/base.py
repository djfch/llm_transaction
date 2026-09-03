"""研报数据模型与聚合接口。

FlashItem 为归一化快讯（金十/律动统一）；CalendarEvent 为日历事件（金十）。
ResearchDataProvider 聚合四类源，向工具层暴露统一只读方法；
任何源失败抛 ResearchSourceError（工具层转中文哨兵，不中断研报轮）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Protocol

# 源标识与快讯类型（落库 timeline 用）
SOURCE_JIN10 = "jin10"
SOURCE_BLOCKBEATS = "blockbeats"
KIND_FLASH = "flash"
KIND_CALENDAR = "calendar"
KIND_INDICATOR = "indicator"


class ResearchSourceError(Exception):
    """数据源失败（网络/鉴权/解析/未配置）。工具层捕获后转中文哨兵。"""


@dataclass
class FlashItem:
    """一条快讯（归一化）：summary 供紧凑注入，detail 为正文全文，raw 供落库。"""

    id: str
    source: str  # jin10 / blockbeats
    title: str
    summary: str  # 紧凑摘要（正文首段截断）
    detail: str  # 正文全文（可能为空）
    url: str
    published_at: float  # Unix 秒
    raw: dict = field(default_factory=dict)


@dataclass
class CalendarEvent:
    """一个经济日历事件（金十）。数值字段原始字符串（''= 缺失）。"""

    title: str
    pub_time: str  # 原始时间串（如 2026-08-05 20:30）
    star: int
    actual: str
    consensus: str
    previous: str
    affect_txt: str


class _Jin10Like(Protocol):
    """金十源接口（鸭子类型）。"""

    async def fetch_calendar(self) -> list[CalendarEvent]:
        """拉取金十经济日历事件列表。

        参数：无

        返回：
            list[CalendarEvent]：日历事件列表（全量返回，日期/星级过滤由调用方负责）
        """
        ...

    async def fetch_flash(self, hours: int = 24) -> list[FlashItem]:
        """拉取金十近若干小时的快讯列表。

        参数：
            hours: int，回溯时间窗口（小时），缺省为 24

        返回：
            list[FlashItem]：归一化快讯列表
        """
        ...

    async def fetch_article_detail(self, item_id: str) -> str:
        """按 id 拉取金十快讯/文章的正文全文。

        参数：
            item_id: str，快讯或文章 id

        返回：
            str：正文全文文本
        """
        ...

    async def search_news(self, keyword: str, limit: int = 20) -> list[FlashItem]:
        """按关键词在金十搜索快讯与文章。

        参数：
            keyword: str，搜索关键词
            limit: int，最多返回条数，缺省为 20

        返回：
            list[FlashItem]：命中的归一化快讯列表
        """
        ...


class _BlockbeatsLike(Protocol):
    """律动源接口（鸭子类型）。"""

    async def fetch_flash(self, hours: int = 24) -> list[FlashItem]:
        """拉取律动近 24 小时的全量快讯。

        参数：
            hours: int，回溯时间窗口（小时）；该源固定返回 24h 全量，参数仅语义化保留

        返回：
            list[FlashItem]：归一化快讯列表
        """
        ...

    async def fetch_indicators(self) -> str:
        """拉取律动指标组快照并拼成统一文本。

        参数：无

        返回：
            str：各指标分节的快照文本（单个指标失败时该节标注"不可用"）
        """
        ...

    async def search_news(self, keyword: str, limit: int = 20) -> list[FlashItem]:
        """按关键词在律动搜索快讯与文章。

        参数：
            keyword: str，搜索关键词
            limit: int，最多返回条数，缺省为 20

        返回：
            list[FlashItem]：命中的归一化快讯列表（按时间倒序）
        """
        ...


class _FredLike(Protocol):
    """FRED 源接口（鸭子类型）。"""

    async def get_macro_series(
        self, indicator: str, look_back: int = 365, end_ts: float | None = None
    ) -> str:
        """拉取一条 FRED 宏观序列并格式化为 Markdown 文本。

        参数：
            indicator: str，宏观指标名（由实现方解析为 FRED 序列 id）
            look_back: int，回溯天数，缺省为 365
            end_ts: float | None，窗口终点 Unix 秒；缺省为当前时间
                （复盘类场景回看历史区间用）

        返回：
            str：Markdown 文本（最新值 + 窗口变化 + 最近观测表格）
        """
        ...


class _PolymarketLike(Protocol):
    """Polymarket 源接口（鸭子类型）。"""

    async def get_prediction_markets(self, topic: str, limit: int = 6) -> str:
        """按主题搜索 Polymarket 未结算预测市场并格式化为文本。

        参数：
            topic: str，搜索主题（如降息、大选）
            limit: int，最多返回市场数，缺省为 6

        返回：
            str：各市场隐含概率、成交额、结算日与近一周变动的文本
        """
        ...


class ResearchDataProvider:
    """研报数据聚合器：向工具层暴露统一只读方法，内部路由到具体源。

    快讯合并：金十 + 律动按 published_at 归并去重（同一秒同标题视为重复）；
    任一源失败返回该源空结果 + 计数（由工具层组装时标注缺失），不抛异常。
    """

    def __init__(
        self,
        *,
        jin10: _Jin10Like | None = None,
        blockbeats: _BlockbeatsLike | None = None,
        fred: _FredLike | None = None,
        polymarket: _PolymarketLike | None = None,
    ) -> None:
        """装配各数据源实例并初始化快讯缓存。

        参数：
            jin10: _Jin10Like | None，金十源实例，缺省 None 表示不装配该源
            blockbeats: _BlockbeatsLike | None，律动源实例，缺省 None 表示不装配该源
            fred: _FredLike | None，FRED 源实例，缺省 None 表示不装配该源
            polymarket: _PolymarketLike | None，Polymarket 源实例，缺省 None 表示不装配该源

        返回：
            None，就地初始化实例属性（含全量快讯缓存 flash_cache，初始为空）
        """
        self._jin10 = jin10
        self._blockbeats = blockbeats
        self._fred = fred
        self._polymarket = polymarket
        # 最近一次拉取的全量快讯缓存（按 id 供 fetch_article_detail 细读）
        self.flash_cache: dict[str, FlashItem] = {}

    @property
    def sources_ready(self) -> list[str]:
        """已装配的源列表（调试/提示用）。

        参数：无
        返回：
            list[str]，已装配的源列表（调试/提示用）
        """
        out = []
        if self._jin10:
            out.append("金十")
        if self._blockbeats:
            out.append("律动")
        if self._fred:
            out.append("FRED")
        if self._polymarket:
            out.append("Polymarket")
        return out

    async def fetch_calendar(self) -> list[CalendarEvent]:
        """金十日历（今日+明日高星事件由工具层过滤）。

        参数：无
        返回：
            list[CalendarEvent]，金十日历（今日+明日高星事件由工具层过滤）
        异常：
            ResearchSourceError，金十日历源未装配时抛出
        """
        if self._jin10 is None:
            raise ResearchSourceError("金十源未装配")
        return await self._jin10.fetch_calendar()

    async def fetch_flash(self, hours: int = 24) -> list[FlashItem]:
        """金十 + 律动 24h 全量快讯合并（按时间正序），更新 flash_cache。

        装配源全部失败时抛 ResearchSourceError（不伪装"无快讯"）；单源失败降级。

        参数：
            hours: int，回溯小时数
        返回：
            list[FlashItem]，金十 + 律动 24h 全量快讯合并（按时间正序），更新 flash_cache
        异常：
            ResearchSourceError，快讯源均未装配或所有已装配来源均失败时抛出
        """
        configured = [s for s in (self._jin10, self._blockbeats) if s is not None]
        if not configured:
            raise ResearchSourceError("快讯数据源未装配")
        cutoff = time.time() - hours * 3600
        items: list[FlashItem] = []
        failed = 0
        seen: set[tuple[float, str]] = set()
        for src in configured:
            try:
                batch = await src.fetch_flash(hours)
            except ResearchSourceError:
                failed += 1
                continue  # 单源失败降级：另一源数据仍可用
            for item in batch:
                if item.published_at < cutoff:
                    continue  # 统一按 hours 窗口过滤（律动固定返回 24h 全量）
                key = (int(item.published_at), item.title[:40])
                if key not in seen:
                    seen.add(key)
                    items.append(item)
        if failed == len(configured):
            raise ResearchSourceError("快讯数据源全部不可用（金十/律动均失败）")
        items.sort(key=lambda x: x.published_at)
        self.flash_cache = {i.id: i for i in items}
        return items

    async def fetch_indicators(self) -> str:
        """律动指标组快照文本。

        参数：无
        返回：
            str，律动指标组快照文本
        异常：
            ResearchSourceError，律动指标源未装配时抛出
        """
        if self._blockbeats is None:
            raise ResearchSourceError("律动指标源未装配")
        return await self._blockbeats.fetch_indicators()

    async def fetch_article_detail(self, item_id: str) -> str:
        """按 id 取全文：先查会话缓存，未命中依次走金十详情、律动重拉兜底。

        参数：
            item_id: str，文章或快讯标识
        返回：
            str，按 id 取全文：先查会话缓存，未命中依次走金十详情、律动重拉兜底
        异常：
            ResearchSourceError，缓存、金十详情及律动重拉均未找到全文时抛出
        """
        cached = self.flash_cache.get(item_id)
        if cached is not None and cached.detail:
            return cached.detail
        if self._jin10 is not None:
            try:
                return await self._jin10.fetch_article_detail(item_id)
            except ResearchSourceError:
                pass  # 金十无此 id（律动文章），继续兜底
        if self._blockbeats is not None:
            try:
                fresh = await self._blockbeats.fetch_flash(24)
            except ResearchSourceError as exc:
                raise ResearchSourceError(
                    f"未找到 id={item_id} 的全文（律动重拉失败：{exc}）"
                ) from exc
            for item in fresh:
                if item.id == item_id and item.detail:
                    return item.detail
        raise ResearchSourceError(f"未找到 id={item_id} 的全文")

    async def search_news(self, keyword: str, limit: int = 20) -> list[FlashItem]:
        """金十 + 律动关键词搜索合并去重（按时间倒序，最新在前）。

        装配源全部失败时抛 ResearchSourceError（不伪装"未找到"）。

        参数：
            keyword: str，新闻搜索关键词
            limit: int，返回数量上限
        返回：
            list[FlashItem]，金十 + 律动关键词搜索合并去重（按时间倒序，最新在前）
        异常：
            ResearchSourceError，搜索源均未装配或所有已装配来源均失败时抛出
        """
        configured = [s for s in (self._jin10, self._blockbeats) if s is not None]
        if not configured:
            raise ResearchSourceError("检索数据源未装配")
        items: list[FlashItem] = []
        failed = 0
        seen: set[tuple[float, str]] = set()
        for src in configured:
            try:
                batch = await src.search_news(keyword, limit=limit)
            except ResearchSourceError:
                failed += 1
                continue
            for item in batch:
                key = (int(item.published_at), item.title[:40])
                if key not in seen:
                    seen.add(key)
                    items.append(item)
        if failed == len(configured):
            raise ResearchSourceError("检索数据源全部不可用（金十/律动均失败）")
        items.sort(key=lambda x: x.published_at, reverse=True)
        # 搜索结果也进缓存：LLM 可从搜索结果细读全文（M10）
        self.flash_cache.update({i.id: i for i in items})
        return items[:limit]

    async def get_macro_series(
        self, indicator: str, look_back: int = 365, end_ts: float | None = None
    ) -> str:
        """FRED 宏观序列。

        参数：
            indicator: str，宏观指标代码
            look_back: int，宏观序列回溯长度
            end_ts: float | None，窗口终点 Unix 秒；缺省为当前时间
        返回：
            str，FRED 宏观序列
        异常：
            ResearchSourceError，FRED 数据源未装配时抛出
        """
        if self._fred is None:
            raise ResearchSourceError("FRED 源未装配")
        return await self._fred.get_macro_series(indicator, look_back, end_ts=end_ts)

    async def get_prediction_markets(self, topic: str, limit: int = 6) -> str:
        """Polymarket 预测概率。

        参数：
            topic: str，预测市场主题
            limit: int，返回数量上限
        返回：
            str，Polymarket 预测概率
        异常：
            ResearchSourceError，Polymarket 数据源未装配时抛出
        """
        if self._polymarket is None:
            raise ResearchSourceError("Polymarket 源未装配")
        return await self._polymarket.get_prediction_markets(topic, limit)
