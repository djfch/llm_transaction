"""整仓计划止损风险的纯 Decimal 计算。"""

from __future__ import annotations

from decimal import Decimal


def planned_stop_loss(
    *, entry_price: Decimal, stop_loss_price: Decimal, size: Decimal, multiplier: Decimal
) -> Decimal:
    """按持仓方向计算从整仓开仓均价到止损价的理论亏损。

    参数：
        entry_price: Decimal，整仓开仓均价
        stop_loss_price: Decimal，整仓止损触发价
        size: Decimal，整仓张数，正数为多仓、负数为空仓
        multiplier: Decimal，每张合约对应的币数

    返回：
        Decimal：非负的计划止损估算；止损已进入盈利区时返回零
    """
    if size > 0:
        distance = max(Decimal(0), entry_price - stop_loss_price)
    elif size < 0:
        distance = max(Decimal(0), stop_loss_price - entry_price)
    else:
        return Decimal(0)
    return distance * abs(size) * multiplier


def projected_position(
    *,
    current_size: Decimal,
    current_entry_price: Decimal,
    added_size: Decimal,
    added_entry_price: Decimal,
) -> tuple[Decimal, Decimal]:
    """计算同方向加仓后的整仓张数与加权开仓均价。

    参数：
        current_size: Decimal，当前持仓张数；无持仓时为零
        current_entry_price: Decimal，当前整仓开仓均价；无持仓时可为零
        added_size: Decimal，本次新增张数，必须与当前持仓同方向
        added_entry_price: Decimal，本次预计入场价

    返回：
        tuple[Decimal, Decimal]：预计整仓张数与加权开仓均价

    异常：
        ValueError：新增张数为零，或与非零当前持仓方向相反时抛出
    """
    if added_size == 0:
        raise ValueError("新增张数不能为零")
    if current_size != 0 and (current_size > 0) != (added_size > 0):
        raise ValueError("只支持同方向加仓，反手必须先平仓")
    if current_size == 0:
        return added_size, added_entry_price
    total_size = current_size + added_size
    weighted = (
        abs(current_size) * current_entry_price + abs(added_size) * added_entry_price
    ) / abs(total_size)
    return total_size, weighted
