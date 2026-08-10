"""Gate 合约持仓量统计读取。"""

from decimal import Decimal
from typing import Any

from gate_api.exceptions import GateApiException

from .errors import wrap_gate_exception
from .market_stats import OpenInterestPoint


class GateOpenInterestMixin:
    """与交易网关主体分文件，避免继续放大超限模块。"""

    _api: Any
    _settle: str

    def fetch_open_interest(self, contract: str) -> Decimal | None:
        """读取最新合约持仓量；空统计返回 None。"""
        try:
            stats = self._api.list_contract_stats(self._settle, contract, limit=1)
        except GateApiException as exc:
            raise wrap_gate_exception(exc) from exc
        if not stats:
            return None
        latest = max(stats, key=lambda stat: int(stat.time or 0))
        return Decimal(str(latest.open_interest or 0))

    def fetch_open_interest_history(
        self, contract: str, interval: str, limit: int = 3
    ) -> list[OpenInterestPoint]:
        """按统计周期读取持仓量历史；过滤不完整点并按时间升序返回。"""
        try:
            stats = self._api.list_contract_stats(
                self._settle, contract, interval=interval, limit=limit
            )
        except GateApiException as exc:
            raise wrap_gate_exception(exc) from exc
        points = [
            OpenInterestPoint(time=int(stat.time), value=Decimal(str(stat.open_interest)))
            for stat in stats
            if stat.time is not None and stat.open_interest is not None
        ]
        return sorted(points, key=lambda point: point.time)
