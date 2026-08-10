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
        return self._oi_provider(contract) if self._oi_provider is not None else None

    def fetch_open_interest_history(
        self, contract: str, interval: str, limit: int = 3
    ) -> list[OpenInterestPoint]:
        if self._oi_history_provider is None:
            return []
        return self._oi_history_provider(contract, interval, limit)
