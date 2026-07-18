"""paper 模式：真实行情驱动的模拟撮合引擎（虚拟账户 + 撮合 + 资金费 + 强平）。"""

from .account import FillRecord, PaperAccount, PaperPosition
from .engine import PaperGateway
from .liquidation import LiquidationEvent

__all__ = [
    "FillRecord",
    "LiquidationEvent",
    "PaperAccount",
    "PaperGateway",
    "PaperPosition",
]
