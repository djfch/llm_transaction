"""把 LLM 给出的保证金与杠杆换算为 Gate 合约张数。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_FLOOR

from src.gateway.base import Contract, MAX_DECIMAL_DIGITS


@dataclass(frozen=True)
class PositionSizing:
    """一次保证金下单换算后的请求值与交易所实际值。"""

    requested_margin: Decimal
    leverage: int
    reference_price: Decimal
    contracts: Decimal
    actual_notional: Decimal
    actual_margin: Decimal
    estimated_fee: Decimal


def _floor_decimal_size(value: Decimal) -> Decimal:
    """把正数向下截到网关可安全序列化的最多 18 位有效数字。

    参数：
        value: Decimal，待截断的正小数张数

    返回：
        Decimal：不大于原值、最多含 18 位有效数字的张数
    """
    places = MAX_DECIMAL_DIGITS - 1 - value.adjusted()
    quantum = Decimal(1).scaleb(-max(places, 0))
    return value.quantize(quantum, rounding=ROUND_DOWN)


def calculate_position_sizing(
    *,
    margin_usdt: Decimal,
    leverage: int,
    reference_price: Decimal,
    direction: int,
    contract: Contract,
    is_market: bool = False,
) -> PositionSizing:
    """用保证金、杠杆和实时合约规格计算实际张数、名义价值与费用。

    参数：
        margin_usdt: Decimal，本单请求投入的保证金
        leverage: int，请求杠杆倍数
        reference_price: Decimal，市价单标记价或限价单委托价
        direction: int，下单方向，1 为多、-1 为空
        contract: Contract，Gate 实时合约规格
        is_market: bool，是否为市价单；市价单使用独立张数上限

    返回：
        PositionSizing：向下取整后的实际下单结果

    异常：
        ValueError：请求值非正、方向非法、张数为零或超出合约最小/最大张数时抛出
    """
    if margin_usdt <= 0:
        raise ValueError("margin_usdt 必须大于 0")
    if leverage <= 0:
        raise ValueError("leverage 必须为正整数")
    if reference_price <= 0:
        raise ValueError("入场参考价必须大于 0")
    if direction not in (-1, 1):
        raise ValueError("方向必须为 long 或 short")
    requested_notional = margin_usdt * Decimal(leverage)
    raw = requested_notional / reference_price / contract.quanto_multiplier
    lots = (
        _floor_decimal_size(raw)
        if contract.enable_decimal
        else raw.to_integral_value(rounding=ROUND_FLOOR)
    )
    if lots == 0:
        raise ValueError("保证金换算后的实际张数为 0，不会自动放大仓位")
    if lots < contract.order_size_min:
        raise ValueError(
            f"实际张数 {lots} 低于合约最小张数 {contract.order_size_min}，不会自动放大仓位"
        )
    maximum = (
        contract.market_order_size_max
        if is_market and contract.market_order_size_max > 0
        else contract.order_size_max
    )
    if lots > maximum:
        label = (
            "市价单最大张数" if is_market and contract.market_order_size_max > 0 else "合约最大张数"
        )
        raise ValueError(f"实际张数 {lots} 超过{label} {maximum}")
    actual_notional = lots * contract.quanto_multiplier * reference_price
    return PositionSizing(
        requested_margin=margin_usdt,
        leverage=leverage,
        reference_price=reference_price,
        contracts=lots * direction,
        actual_notional=actual_notional,
        actual_margin=actual_notional / Decimal(leverage),
        estimated_fee=actual_notional * contract.taker_fee_rate,
    )


def calculate_reduction_size(
    position_size: Decimal, reduce_pct: Decimal, contract: Contract | None
) -> Decimal:
    """按持仓张数和减仓比例计算不会超过请求比例的内部反向张数。

    参数：
        position_size: Decimal，当前持仓张数，正多负空
        reduce_pct: Decimal，减仓比例，必须位于 0 与 1 之间
        contract: Contract | None，当前持仓的内存合约规格；缺失时按整数张安全降级

    返回：
        Decimal：与持仓方向相反的减仓张数

    异常：
        ValueError：无持仓、比例越界或取整后为零时抛出
    """
    if position_size == 0:
        raise ValueError("当前无持仓，无法减仓")
    if reduce_pct <= 0 or reduce_pct >= 1:
        raise ValueError("reduce_pct 必须在 0 与 1 之间；整仓平仓请用 close=true")
    raw = abs(position_size) * reduce_pct
    lots = (
        _floor_decimal_size(raw)
        if contract is not None and contract.enable_decimal
        else raw.to_integral_value(rounding=ROUND_FLOOR)
    )
    if lots == 0:
        raise ValueError("减仓比例换算后的实际张数为 0，请提高比例或使用 close=true")
    if contract is not None and lots < contract.order_size_min:
        raise ValueError(f"减仓后的实际张数 {lots} 低于合约最小张数 {contract.order_size_min}")
    return -lots if position_size > 0 else lots
