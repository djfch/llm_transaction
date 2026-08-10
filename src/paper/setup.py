"""PaperGateway 装配：把公共行情读取能力转发给模拟撮合层。"""

from collections.abc import Callable
from typing import Protocol

from ..config import PaperConfig
from ..gateway.base import Candle, Contract
from .engine import PaperGateway


class PublicMarketGateway(Protocol):
    def get_candlesticks(self, *args: object, **kwargs: object) -> list[Candle]:
        """读取合约 K 线数据，查询条件原样透传给底层行情源。

        参数：
            *args: object，透传给底层行情网关的位置参数（合约、周期、条数等）
            **kwargs: object，透传给底层行情网关的关键字参数（合约、周期、条数等）

        返回：
            list[Candle]：K 线数据列表
        """
        ...

    def fetch_open_interest(self, contract: str):
        """读取合约最新持仓量。

        参数：
            contract: str，合约名（如 BTC_USDT）

        返回：
            Decimal | None：持仓量张数；交易所无该数据时返回 None
        """
        ...

    def fetch_open_interest_history(self, contract: str, interval: str, limit: int = 3):
        """按统计周期读取合约持仓量历史。

        参数：
            contract: str，合约名（如 BTC_USDT）
            interval: str，统计周期（如 5m、1h）
            limit: int，返回的最大数据点数，省略时默认 3

        返回：
            list[OpenInterestPoint]：持仓量历史数据点列表
        """
        ...


def build_paper_gateway(
    config: PaperConfig,
    contracts: list[Contract],
    candle_provider: Callable[..., list[Candle]] | None,
    public: PublicMarketGateway | None,
) -> PaperGateway:
    """创建 paper 网关，并注入同一个公共 Gate 行情源的 K 线与持仓量读取。

    参数：
        config: PaperConfig，模拟账户配置
        contracts: list[Contract]，需要处理的合约列表
        candle_provider: Callable[..., list[Candle]] | None，可选的历史 K 线读取回调
        public: PublicMarketGateway | None，可选的公共行情网关

    返回：
        PaperGateway：创建 paper 网关，并注入同一个公共 Gate 行情源的 K 线与持仓量读取
    """
    provider = candle_provider or (public.get_candlesticks if public else None)
    gateway = PaperGateway(
        config,
        candle_provider=provider,
        oi_provider=public.fetch_open_interest if public else None,
        oi_history_provider=public.fetch_open_interest_history if public else None,
    )
    for contract in contracts:
        gateway.upsert_contract(contract)
    return gateway
