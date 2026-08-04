"""OpenInterestCache 测试：可控假网关验证刷新、容错与 watchlist 共享引用。"""

from decimal import Decimal

from src.gateway.base import GatewayError
from src.market.oi import OpenInterestCache

BTC = "BTC_USDT"
ETH = "ETH_USDT"


class FakeOiGateway:
    """可控 OI 数据源：values 给返回值，fail 集合内合约抛 GatewayError。"""

    def __init__(
        self, values: dict[str, Decimal] | None = None, fail: tuple[str, ...] = ()
    ) -> None:
        self._values = values or {}
        self._fail = set(fail)

    def fetch_open_interest(self, contract: str) -> Decimal | None:
        if contract in self._fail:
            raise GatewayError("模拟拉取失败")
        return self._values.get(contract)


async def test_refresh_once_populates_cache():
    watchlist = [BTC, ETH]
    cache = OpenInterestCache(FakeOiGateway({BTC: Decimal("111"), ETH: Decimal("222")}), watchlist)
    await cache.refresh_once()
    assert cache.get(BTC) == Decimal("111")
    assert cache.get(ETH) == Decimal("222")
    assert cache.get("SOL_USDT") is None


async def test_single_failure_keeps_old_value_and_others():
    watchlist = [BTC, ETH]
    gateway = FakeOiGateway({BTC: Decimal("111"), ETH: Decimal("222")})
    cache = OpenInterestCache(gateway, watchlist)
    await cache.refresh_once()
    # 第二轮 BTC 失败、ETH 新值：BTC 保留旧值，ETH 更新
    gateway._fail = {BTC}
    gateway._values = {ETH: Decimal("333")}
    await cache.refresh_once()
    assert cache.get(BTC) == Decimal("111")
    assert cache.get(ETH) == Decimal("333")


async def test_none_result_does_not_overwrite():
    watchlist = [BTC]
    gateway = FakeOiGateway({BTC: Decimal("111")})
    cache = OpenInterestCache(gateway, watchlist)
    await cache.refresh_once()
    gateway._values = {}  # 数据源返回 None（如 paper 不支持）
    await cache.refresh_once()
    assert cache.get(BTC) == Decimal("111")


async def test_watchlist_shared_reference():
    watchlist = [BTC]
    gateway = FakeOiGateway({BTC: Decimal("111"), ETH: Decimal("222")})
    cache = OpenInterestCache(gateway, watchlist)
    watchlist.append(ETH)  # 装配层原地更新自选，缓存自动跟随
    await cache.refresh_once()
    assert cache.get(ETH) == Decimal("222")
