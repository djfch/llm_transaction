"""Paper 网关的公共持仓量数据委托。"""

from collections.abc import Callable
from decimal import Decimal

from ..gateway.base import OpenInterestPoint

OpenInterestProvider = Callable[[str], Decimal | None]
OpenInterestHistoryProvider = Callable[[str, str, int], list[OpenInterestPoint]]


class PaperOpenInterestMixin:
    """paper 不伪造持仓量，只转发启动时注入的公共行情源。"""

    _oi_provider: OpenInterestProvider | None
    _oi_history_provider: OpenInterestHistoryProvider | None

    def fetch_open_interest(self, contract: str) -> Decimal | None:
        """读取合约最新持仓量；paper 不自行生成数据，只转发启动时注入的公共行情源。

        参数：
            contract: str，合约名（如 BTC_USDT）

        返回：
            Decimal | None：持仓量张数；未注入持仓量数据源或行情源无该数据时返回 None
        """
        return self._oi_provider(contract) if self._oi_provider is not None else None

    def fetch_open_interest_history(
        self, contract: str, interval: str, limit: int = 3
    ) -> list[OpenInterestPoint]:
        """读取合约持仓量历史；paper 不自行生成数据，只转发启动时注入的公共行情源。

        参数：
            contract: str，合约名（如 BTC_USDT）
            interval: str，统计周期（如 5m、1h、1d），透传给交易所统计接口
            limit: int，返回的最大数据点数，省略时默认取最近 3 个点

        返回：
            list[OpenInterestPoint]：持仓量快照列表；未注入持仓量数据源时返回空列表
        """
        if self._oi_history_provider is None:
            return []
        return self._oi_history_provider(contract, interval, limit)
