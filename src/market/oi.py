"""合约持仓量（OI）缓存：按需刷新 + 读取，不做后台循环（循环调度由装配层负责）。"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Protocol

from ..audit.logger import get_logger

logger = get_logger(__name__)


class OiGateway(Protocol):
    """OI 数据源鸭子类型：真实网关实现；paper 返回 None（无此数据）。"""

    def fetch_open_interest(self, contract: str) -> Decimal | None:
        """读取合约最新持仓量。

        参数：
            contract: str，合约名（如 BTC_USDT）

        返回：
            Decimal | None：持仓量张数；数据源不支持该数据时返回 None（如 paper 网关）
        """
        ...


class OpenInterestCache:
    """按合约缓存最新持仓量：refresh_once 逐合约拉取，单个失败不影响其他。"""

    def __init__(self, gateway: OiGateway, watchlist: list[str]) -> None:
        """初始化持仓量缓存：记录数据源与自选列表，缓存起始为空。

        参数：
            gateway: OiGateway，持仓量数据源（真实网关实现；paper 网关返回 None）
            watchlist: list[str]，要跟踪的自选合约列表；与装配层共享引用，原地更新即生效

        返回：
            None，就地初始化实例属性（持仓量缓存为空字典）
        """
        self._gateway = gateway
        self._watchlist = watchlist  # 共享引用：装配层原地更新自选即生效
        self._oi: dict[str, Decimal] = {}

    async def refresh_once(self) -> None:
        """拉一轮并写缓存；失败记 warning 保留旧值，None（数据源不支持）不覆盖。

        参数：无

        返回：
            None，拉一轮并写缓存；失败记 warning 保留旧值，None（数据源不支持）不覆盖
        """
        for contract in self._watchlist:
            try:
                # 网关是同步 SDK 调用，移出事件循环线程执行（同 fill_sync 先例）
                value = await asyncio.to_thread(self._gateway.fetch_open_interest, contract)
            except Exception:
                logger.warning("持仓量拉取失败（%s），保留旧值", contract, exc_info=True)
                continue
            if value is not None:
                self._oi[contract] = value

    def get(self, contract: str) -> Decimal | None:
        """最新缓存值；从未拉取成功或数据源不支持时为 None。

        参数：
            contract: str，合约标识

        返回：
            Decimal | None，最新缓存值；从未拉取成功或数据源不支持时为 None
        """
        return self._oi.get(contract)
