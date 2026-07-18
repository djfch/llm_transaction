"""交易所网关：统一接口（base）、真实实现（gate_rest）、内存 mock（mock）。"""

from .base import (
    Account,
    Candle,
    Contract,
    ContractNotFound,
    Gateway,
    GatewayError,
    OrderNotFound,
    OrderRequest,
    OrderResult,
    OrderStateUnknown,
    Position,
    PositionNotFound,
    Ticker,
)
from .gate_rest import GateRestGateway
from .mock import MockGateway

__all__ = [
    "Account",
    "Candle",
    "Contract",
    "ContractNotFound",
    "GateRestGateway",
    "Gateway",
    "GatewayError",
    "MockGateway",
    "OrderNotFound",
    "OrderRequest",
    "OrderResult",
    "OrderStateUnknown",
    "Position",
    "PositionNotFound",
    "Ticker",
]
