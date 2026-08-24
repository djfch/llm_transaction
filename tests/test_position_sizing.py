"""保证金到 Gate 张数的纯 Decimal 换算测试。"""

from decimal import Decimal

import pytest

from src.agent.position_sizing import calculate_position_sizing, calculate_reduction_size
from src.gateway.base import Contract

D = Decimal


def _contract(
    *,
    decimal_size: bool = False,
    minimum: str = "1",
    maximum: str = "1000",
    market_maximum: str = "0",
) -> Contract:
    """构造固定价格与乘数的合约规格。

    参数：
        decimal_size: bool，是否支持小数张
        minimum: str，最小下单张数
        maximum: str，最大下单张数
        market_maximum: str，市价单独立最大张数；0 表示沿用普通上限

    返回：
        Contract：标记价 100、每张 0.1 币的测试合约
    """
    return Contract(
        name="BTC_USDT",
        quanto_multiplier=D("0.1"),
        order_size_min=D(minimum),
        order_size_max=D(maximum),
        market_order_size_max=D(market_maximum),
        order_price_round=D("0.1"),
        enable_decimal=decimal_size,
        mark_price=D(100),
        funding_rate=D(0),
        funding_interval=28800,
        maker_fee_rate=D("0.0002"),
        taker_fee_rate=D("0.0005"),
        status="trading",
        in_delisting=False,
    )


def test_integer_contract_size_is_floored_and_recalculated():
    """校验整数张合约向下取整，并按实际张数重算保证金与手续费。

    参数：无

    返回：
        None，断言 25 USDT × 3 倍换算为 7 张而不是向上放大到 8 张
    """
    result = calculate_position_sizing(
        margin_usdt=D(25),
        leverage=3,
        reference_price=D(100),
        direction=1,
        contract=_contract(),
    )
    assert result.contracts == D(7)
    assert result.actual_notional == D(70)
    assert result.actual_margin == D(70) / D(3)
    assert result.estimated_fee == D("0.0350")


def test_decimal_contract_keeps_decimal_protocol_and_short_sign():
    """校验支持小数张的合约保留十进制张数并在空单上添加负号。

    参数：无

    返回：
        None，断言请求换算为负 7.5 张且没有整数化
    """
    result = calculate_position_sizing(
        margin_usdt=D(25),
        leverage=3,
        reference_price=D(100),
        direction=-1,
        contract=_contract(decimal_size=True, minimum="0.1"),
    )
    assert result.contracts == D("-7.5")
    assert result.actual_margin == D(25)


def test_market_order_uses_independent_size_limit_but_limit_order_uses_normal_limit():
    """校验 Gate 市价单独立张数上限只约束市价请求。

    参数：无

    返回：
        None，断言 101 张市价单被 100 张上限拒绝，而同张数限价单可换算
    """
    contract = _contract(maximum="1000", market_maximum="100")
    with pytest.raises(ValueError, match="市价单最大张数"):
        calculate_position_sizing(
            margin_usdt=D(1010),
            leverage=1,
            reference_price=D(100),
            direction=1,
            contract=contract,
            is_market=True,
        )
    result = calculate_position_sizing(
        margin_usdt=D(1010),
        leverage=1,
        reference_price=D(100),
        direction=1,
        contract=contract,
        is_market=False,
    )
    assert result.contracts == D(101)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"margin_usdt": D(0)}, "margin_usdt"),
        ({"leverage": 0}, "leverage"),
        ({"reference_price": D(0)}, "参考价"),
        ({"direction": 0}, "方向"),
        ({"margin_usdt": D("0.1")}, "实际张数为 0"),
        ({"margin_usdt": D(10), "contract": _contract(minimum="2")}, "低于合约最小"),
        ({"margin_usdt": D(20000)}, "超过合约最大"),
    ],
)
def test_invalid_or_out_of_range_sizing_is_rejected(kwargs: dict, message: str):
    """校验非法请求、零张、低于最小张和超过最大张均明确拒绝。

    参数：
        kwargs: dict，覆盖默认换算入参
        message: str，预期错误消息片段

    返回：
        None，断言换算函数抛出对应 ValueError
    """
    params = {
        "margin_usdt": D(10),
        "leverage": 1,
        "reference_price": D(100),
        "direction": 1,
        "contract": _contract(),
    }
    with pytest.raises(ValueError, match=message):
        calculate_position_sizing(**(params | kwargs))


def test_reduction_size_uses_opposite_direction_and_floors_integer_position():
    """校验部分减仓生成反向张数，整数仓位不会因比例产生小数张。

    参数：无

    返回：
        None，断言多仓减仓为负张、空仓减仓为正张
    """
    assert calculate_reduction_size(D(10), D("0.25"), _contract()) == D(-2)
    assert calculate_reduction_size(D(-10), D("0.25"), _contract()) == D(2)
    decimal_contract = _contract(decimal_size=True, minimum="0.1")
    assert calculate_reduction_size(D("1.5"), D("0.5"), decimal_contract) == D("-0.75")
    assert calculate_reduction_size(D(1), D("0.5"), decimal_contract) == D("-0.5")


@pytest.mark.parametrize(
    ("size", "pct", "message"),
    [
        (D(0), D("0.5"), "无持仓"),
        (D(10), D(0), "0 与 1"),
        (D(10), D(1), "close=true"),
        (D(1), D("0.5"), "实际张数为 0"),
    ],
)
def test_invalid_reduction_size_is_rejected(size: Decimal, pct: Decimal, message: str):
    """校验无仓、比例越界和整数取整为零的减仓请求被拒绝。

    参数：
        size: Decimal，当前持仓张数
        pct: Decimal，请求减仓比例
        message: str，预期错误片段

    返回：
        None，断言计算函数抛出对应 ValueError
    """
    with pytest.raises(ValueError, match=message):
        calculate_reduction_size(size, pct, _contract())
