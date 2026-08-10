"""PaperGateway 装配：把公共行情读取能力转发给模拟撮合层。"""

from collections.abc import Callable
from typing import Protocol

from ..config import PaperConfig
from ..gateway.base import Candle, Contract
from .engine import PaperGateway


class PublicMarketGateway(Protocol):
    def get_candlesticks(self, *args: object, **kwargs: object) -> list[Candle]: ...

    def fetch_open_interest(self, contract: str): ...

    def fetch_open_interest_history(self, contract: str, interval: str, limit: int = 3): ...


def build_paper_gateway(
    config: PaperConfig,
    contracts: list[Contract],
    candle_provider: Callable[..., list[Candle]] | None,
    public: PublicMarketGateway | None,
) -> PaperGateway:
    """创建 paper 网关，并注入同一个公共 Gate 行情源的 K 线与持仓量读取。"""
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
