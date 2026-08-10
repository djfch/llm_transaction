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
        """初始化假 OI 数据源：登记可返回的持仓量与需模拟失败的合约。

        参数：
            values: dict[str, Decimal] | None，各合约返回的持仓量；缺省视为无数据
            fail: tuple[str, ...]，拉取时抛 GatewayError 的合约集合，用于模拟失败

        返回：
            None，就地初始化实例属性（返回值表与失败集合）
        """
        self._values = values or {}
        self._fail = set(fail)

    def fetch_open_interest(self, contract: str) -> Decimal | None:
        """按合约返回预设持仓量，命中失败集合时模拟网关报错。

        参数：
            contract: str，合约名（如 BTC_USDT）

        返回：
            Decimal | None：预设的持仓量；未预设时返回 None（模拟 paper 不支持）

        异常：
            GatewayError：contract 在构造时的 fail 集合中，模拟拉取失败
        """
        if contract in self._fail:
            raise GatewayError("模拟拉取失败")
        return self._values.get(contract)


async def test_refresh_once_populates_cache():
    """校验一轮刷新能把各合约持仓量写入缓存，未拉取的合约读到 None。

    参数：无

    返回：
        None，断言 refresh_once 后 BTC/ETH 缓存值与假网关一致，未跟踪的 SOL_USDT 为 None
    """
    watchlist = [BTC, ETH]
    cache = OpenInterestCache(FakeOiGateway({BTC: Decimal("111"), ETH: Decimal("222")}), watchlist)
    await cache.refresh_once()
    assert cache.get(BTC) == Decimal("111")
    assert cache.get(ETH) == Decimal("222")
    assert cache.get("SOL_USDT") is None


async def test_single_failure_keeps_old_value_and_others():
    """校验单个合约拉取失败时保留其旧值，其余合约照常更新。

    参数：无

    返回：
        None，断言第二轮刷新 BTC 失败仍保留旧值 111、ETH 更新为新值 333
    """
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
    """校验数据源返回 None（如 paper 不支持 OI）时不覆盖已有缓存值。

    参数：无

    返回：
        None，断言第二轮刷新数据源返回 None 后，BTC 缓存仍为首轮旧值 111
    """
    watchlist = [BTC]
    gateway = FakeOiGateway({BTC: Decimal("111")})
    cache = OpenInterestCache(gateway, watchlist)
    await cache.refresh_once()
    gateway._values = {}  # 数据源返回 None（如 paper 不支持）
    await cache.refresh_once()
    assert cache.get(BTC) == Decimal("111")


async def test_watchlist_shared_reference():
    """校验缓存与装配层共享 watchlist 引用，自选原地追加后刷新自动跟踪新合约。

    参数：无

    返回：
        None，断言构造后向 watchlist 原地追加 ETH，刷新后 ETH 持仓量已入缓存
    """
    watchlist = [BTC]
    gateway = FakeOiGateway({BTC: Decimal("111"), ETH: Decimal("222")})
    cache = OpenInterestCache(gateway, watchlist)
    watchlist.append(ETH)  # 装配层原地更新自选，缓存自动跟随
    await cache.refresh_once()
    assert cache.get(ETH) == Decimal("222")
