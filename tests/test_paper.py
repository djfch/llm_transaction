"""PaperGateway 模拟撮合引擎测试：盈亏、手续费、资金费、强平、保证金占用/释放。"""

from decimal import Decimal

import pytest

from src.config import PaperConfig
from src.gateway.base import Contract, GatewayError, OrderNotFound, OrderRequest
from src.paper import PaperGateway

BTC = "BTC_USDT"
D = Decimal


def make_contract(taker: str = "0.0005", maker: str = "0.0002") -> Contract:
    return Contract(
        name=BTC,
        quanto_multiplier=D("1"),
        order_size_min=D(1),
        order_size_max=D("1000000"),
        order_price_round=D("0.1"),
        enable_decimal=True,
        mark_price=D("100"),
        funding_rate=D("0.0001"),
        funding_interval=28800,
        maker_fee_rate=D(maker),
        taker_fee_rate=D(taker),
        status="trading",
        in_delisting=False,
    )


def make_gateway(slippage: str = "0", taker: str = "0.0005") -> PaperGateway:
    cfg = PaperConfig(initial_equity=10000.0, slippage=float(slippage))
    gw = PaperGateway(cfg, contracts={BTC: make_contract(taker=taker)}, maintenance_rate=D("0.005"))
    gw.on_price(BTC, D("100"), D("99.9"), D("100.1"))
    return gw


def buy(gw: PaperGateway, size, price=None):
    return gw.place_order(OrderRequest(contract=BTC, size=D(size), price=price))


def close_all(gw: PaperGateway):
    return gw.place_order(OrderRequest(contract=BTC, close=True))


def test_long_profit():
    gw = make_gateway()
    buy(gw, 10)
    assert gw.account.available == D("8999.5")  # 10000 - 1000 保证金 - 0.5 taker 费
    gw.on_price(BTC, D("110"), D("109.9"), D("110.1"))
    assert gw.list_positions()[0].unrealised_pnl == D("100")
    result = close_all(gw)
    assert result.fill_price == D("110")
    assert gw.account.available == D("10098.95")  # +100 盈亏 - 0.55 平仓费
    assert gw.account.total_realized == D("100")


def test_long_loss():
    gw = make_gateway()
    buy(gw, 10)
    gw.on_price(BTC, D("90"), D("89.9"), D("90.1"))
    close_all(gw)
    assert gw.account.available == D("9899.05")  # -100 盈亏 - 0.5 - 0.45 费
    assert gw.account.total_realized == D("-100")


def test_short_profit():
    gw = make_gateway()
    buy(gw, -10)
    gw.on_price(BTC, D("90"), D("89.9"), D("90.1"))
    close_all(gw)
    assert gw.account.available == D("10099.05")  # +100 盈亏 - 0.5 - 0.45 费


def test_short_loss():
    gw = make_gateway()
    buy(gw, -10)
    gw.on_price(BTC, D("110"), D("109.9"), D("110.1"))
    close_all(gw)
    assert gw.account.available == D("9898.95")


def test_fee_taker_and_maker():
    gw = make_gateway()
    result = buy(gw, 10, price=D("99"))  # 低于 ask 100.1 → 挂单
    assert result.status == "open" and result.left == D("10")
    gw.on_price(BTC, D("99"), D("98.9"), D("99"))
    fill = gw.account.fills[-1]
    assert fill.maker is True and fill.price == D("99")
    assert fill.fee == D("0.198")  # 10 × 99 × 0.0002
    close_all(gw)  # 市价平仓按 taker：10 × 99 × 0.0005
    assert gw.account.total_fee == D("0.198") + D("0.495")


def test_limit_conservative_fill_price():
    gw = make_gateway()
    result = buy(gw, 10, price=D("100.1"))  # ≥ ask → 立即成交，吃对手价而非更优价
    assert result.status == "finished" and result.fill_price == D("100.1")
    assert gw.account.fills[-1].maker is False
    result = buy(gw, -10, price=D("99.9"))  # ≤ bid → 立即成交于 bid
    assert result.fill_price == D("99.9")


def test_limit_ioc_cancelled_when_not_crossed():
    gw = make_gateway()
    result = gw.place_order(OrderRequest(contract=BTC, size=D(10), price=D("99"), tif="ioc"))
    assert result.status == "finished" and result.finish_as == "cancelled"
    assert gw.list_orders(BTC) == []


def test_market_slippage():
    gw = make_gateway(slippage="0.001")
    result = buy(gw, 10)
    assert result.fill_price == D("100.1")  # mark × (1 + 0.001)
    result = buy(gw, -5)
    assert result.fill_price == D("99.9")  # mark × (1 - 0.001)


def test_funding_long_pays_short_receives():
    gw = make_gateway()
    buy(gw, 10)
    delta = gw.settle_funding(BTC, D("0.001"))  # 正费率：多头付 0.001 × 1000
    assert delta == D("-1")
    assert gw.account.available == D("8998.5")
    assert gw.account.total_funding == D("-1")
    buy(gw, -20)  # 平多并翻空 10 张
    delta = gw.settle_funding(BTC, D("0.001"))  # 正费率：空头收
    assert delta == D("1")


def test_funding_negative_rate():
    gw = make_gateway()
    buy(gw, 10)
    assert gw.settle_funding(BTC, D("-0.001")) == D("1")  # 负费率：多头收
    assert gw.settle_funding(BTC, D("0")) == D("0")


def test_liquidation():
    gw = make_gateway(taker="0")
    gw.set_leverage(BTC, 10)
    buy(gw, 10)  # 保证金 = 1000 / 10 = 100
    assert gw.account.available == D("9900")
    gw.on_price(BTC, D("95"), D("94.9"), D("95.1"))  # 保证金率 50/950 > 0.005
    assert gw.liquidations == []
    gw.on_price(BTC, D("90"), D("89.9"), D("90.1"))  # 保证金率 0 → 强平
    assert gw.list_positions() == []
    event = gw.liquidations[0]
    assert event.size == D("10") and event.loss == D("100")
    assert event.returned_margin == D("0")
    assert gw.account.available == D("9900")  # 保证金全部亏损，无返还


def test_average_entry_on_add():
    gw = make_gateway()
    buy(gw, 5)
    gw.on_price(BTC, D("120"), D("119.9"), D("120.1"))
    buy(gw, 5)
    pos = gw.list_positions()[0]
    assert pos.size == D("10") and pos.entry_price == D("110")
    assert pos.margin == D("1100")  # 500 + 600


def test_close_releases_margin():
    gw = make_gateway()
    buy(gw, 10)
    assert gw.account.positions[BTC].margin == D("1000")
    close_all(gw)
    assert gw.list_positions() == []
    assert gw.account.positions[BTC].margin == D("0")
    assert gw.account.available == D("9999")  # 保证金全额释放，仅扣两次手续费


def test_insufficient_balance_rejected():
    gw = make_gateway()
    with pytest.raises(GatewayError):
        buy(gw, 200)  # 需 20000 保证金 > 10000 可用


def test_set_leverage_reduces_margin():
    gw = make_gateway()
    gw.set_leverage(BTC, 5)
    buy(gw, 10)
    pos = gw.list_positions()[0]
    assert pos.leverage == D("5") and pos.margin == D("200")


def test_cancel_order():
    gw = make_gateway()
    result = buy(gw, 10, price=D("99"))
    cancelled = gw.cancel_order(BTC, result.id)
    assert cancelled.finish_as == "cancelled"
    assert gw.list_orders(BTC) == []
    with pytest.raises(OrderNotFound):
        gw.cancel_order(BTC, result.id)


def test_account_position_interface():
    gw = make_gateway()
    buy(gw, 10)
    account = gw.get_account()
    assert account.available == D("8999.5") and account.unrealised_pnl == D("0")
    gw.on_price(BTC, D("110"), D("109.9"), D("110.1"))
    pos = gw.list_positions()[0]
    assert (pos.mark_price, pos.unrealised_pnl, pos.margin, pos.leverage) == (
        D("110"),
        D("100"),
        D("1000"),
        D("1"),
    )
    assert gw.get_account().unrealised_pnl == D("100")
    assert gw.equity() == D("10099.5")
    assert gw.get_tickers()[0].mark_price == D("110")


def test_candle_provider_injection():
    gw = make_gateway()
    assert gw.get_candlesticks(BTC) == []  # 无 provider 默认空
    sentinel = [object()]
    gw2 = PaperGateway(
        PaperConfig(), contracts={BTC: make_contract()}, candle_provider=lambda *args: sentinel
    )  # 外部行情缓存代理注入点
    assert gw2.get_candlesticks(BTC) is sentinel
    with pytest.raises(ValueError):
        gw2.get_candlesticks(BTC, limit=10, from_ts=1)  # limit 与 from/to 互斥


def test_reduce_only_requires_opposite_position():
    gw = make_gateway()
    with pytest.raises(GatewayError):
        gw.place_order(OrderRequest(contract=BTC, size=D(-5), reduce_only=True))


def test_close_without_position_is_noop():
    gw = make_gateway()
    result = close_all(gw)
    assert result.status == "finished" and result.finish_as == "no_position"
    assert result.left == D("0") and gw.account.fills == []  # 无持仓不记假成交
