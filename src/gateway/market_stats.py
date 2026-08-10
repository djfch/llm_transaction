"""公共合约统计模型：研报市场快照使用的持仓量历史点。"""

from __future__ import annotations

from decimal import Decimal
from pydantic import BaseModel


class OpenInterestPoint(BaseModel):
    """某个统计周期的持仓量快照。time 为秒级时间戳。"""

    time: int
    value: Decimal
