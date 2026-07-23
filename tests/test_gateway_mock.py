"""MockGateway 单元测试，以及 gate_rest 纯业务函数（下单语义组装/异常分类）测试。"""

from decimal import Decimal

import pytest
from gate_api.exceptions import GateApiException

from src.gateway import (
    Account,
    Contract,
    ContractNotFound,
    GatewayError,
    MockGateway,
    OrderNotFound,
    OrderRequest,
)
from src.gateway.base import Candle
from src.gateway.gate_rest import build_order_payload, gen_client_order_id, wrap_gate_exception

BTC = "BTC_USDT"


def make_contract(mark_price: str = "50000") -> Contract:
    return Contract(
        name=BTC,
        quanto_multiplier=Decimal("0.0001"),
        order_size_min=Decimal(1),
        order_size_max=Decimal("1000000"),
        order_price_round=Decimal("0.1"),
        enable_decimal=False,
        mark_price=Decimal(mark_price),
        funding_rate=Decimal("0.0001"),
        funding_interval=28800,
        maker_fee_rate=Decimal("0.0002"),
        taker_fee_rate=Decimal("0.0005"),
        status="trading",
        in_delisting=False,
    )


@pytest.fixture
def gw() -> MockGateway:
    return MockGateway(contracts={BTC: make_contract()})


# ---------- 基础查询 ----------


def test_get_contract(gw: MockGateway):
    assert gw.get_contract(BTC).mark_price == Decimal("50000")


def test_get_contract_not_found(gw: MockGateway):
    with pytest.raises(ContractNotFound):
        gw.get_contract("ETH_USDT")


def test_default_account(gw: MockGateway):
    assert gw.get_account().available == Decimal("10000")


def test_custom_account():
    gw = MockGateway(account=Account(available=Decimal("5"), unrealised_pnl=Decimal("1")))
    assert gw.get_account().unrealised_pnl == Decimal(1)


# ---------- 市价单与持仓聚合 ----------


def test_market_order_fills_at_mark_price(gw: MockGateway):
    result = gw.place_order(OrderRequest(contract=BTC, size=Decimal(2)))
    assert result.status == "finished"
    assert result.fill_price == Decimal("50000")
    assert result.left == Decimal(0)
    pos = gw.positions[BTC]
    assert pos.size == Decimal(2)
    assert pos.entry_price == Decimal("50000")


def test_add_to_long_uses_weighted_entry(gw: MockGateway):
    gw.place_order(OrderRequest(contract=BTC, size=Decimal(1)))
    gw.contracts[BTC] = make_contract("51000")
    gw.place_order(OrderRequest(contract=BTC, size=Decimal(1)))
    pos = gw.positions[BTC]
    assert pos.size == Decimal(2)
    assert pos.entry_price == Decimal("50500")


def test_short_position_negative_size(gw: MockGateway):
    gw.place_order(OrderRequest(contract=BTC, size=Decimal(-3)))
    assert gw.positions[BTC].size == Decimal(-3)


def test_reduce_position(gw: MockGateway):
    gw.place_order(OrderRequest(contract=BTC, size=Decimal(3)))
    gw.place_order(OrderRequest(contract=BTC, size=Decimal(-1), reduce_only=True))
    assert gw.positions[BTC].size == Decimal(2)


def test_flip_position_new_entry(gw: MockGateway):
    gw.place_order(OrderRequest(contract=BTC, size=Decimal(1)))
    gw.contracts[BTC] = make_contract("49000")
    gw.place_order(OrderRequest(contract=BTC, size=Decimal(-3)))
    pos = gw.positions[BTC]
    assert pos.size == Decimal(-2)
    assert pos.entry_price == Decimal("49000")  # 翻仓部分以成交价开仓


def test_close_true_closes_entire_position(gw: MockGateway):
    gw.place_order(OrderRequest(contract=BTC, size=Decimal(3)))
    result = gw.place_order(OrderRequest(contract=BTC, close=True))
    assert result.status == "finished"
    assert gw.positions[BTC].size == Decimal(0)
    assert gw.list_positions() == []


# ---------- reduce_only 方向校验（对齐真实 Gate 与 paper 引擎） ----------


def test_reduce_only_no_position_rejected(gw: MockGateway):
    with pytest.raises(GatewayError) as excinfo:
        gw.place_order(OrderRequest(contract=BTC, size=Decimal(-1), reduce_only=True))
    assert excinfo.value.label == "REDUCE_ONLY"
    assert gw.list_positions() == []  # 未产生持仓


def test_reduce_only_same_direction_long_rejected(gw: MockGateway):
    gw.place_order(OrderRequest(contract=BTC, size=Decimal(2)))  # 多仓
    with pytest.raises(GatewayError, match="同向或无持仓"):
        gw.place_order(OrderRequest(contract=BTC, size=Decimal(1), reduce_only=True))
    assert gw.positions[BTC].size == Decimal(2)  # 未加仓


def test_reduce_only_same_direction_short_rejected(gw: MockGateway):
    """空仓 + reduce_only 卖单会同向加仓，必须拒绝（已确认缺陷 P3-#25）。"""
    gw.place_order(OrderRequest(contract=BTC, size=Decimal(-2)))  # 空仓
    with pytest.raises(GatewayError) as excinfo:
        gw.place_order(OrderRequest(contract=BTC, size=Decimal(-1), reduce_only=True))
    assert excinfo.value.label == "REDUCE_ONLY"
    assert gw.positions[BTC].size == Decimal(-2)  # 未加仓


def test_reduce_only_limit_without_position_rejected(gw: MockGateway):
    with pytest.raises(GatewayError):
        gw.place_order(
            OrderRequest(contract=BTC, size=Decimal(-1), price=Decimal("49000"), reduce_only=True)
        )


# ---------- 限价单生命周期 ----------


def test_limit_order_stays_open(gw: MockGateway):
    result = gw.place_order(OrderRequest(contract=BTC, size=Decimal(1), price=Decimal("49000")))
    assert result.status == "open"
    assert result.left == Decimal(1)
    assert gw.list_orders(BTC, "open")[0].id == result.id


def test_amend_order(gw: MockGateway):
    order = gw.place_order(OrderRequest(contract=BTC, size=Decimal(1), price=Decimal("49000")))
    amended = gw.amend_order(BTC, order.id, price=Decimal("48000"), size=Decimal(2))
    assert amended.left == Decimal(2)


def test_amend_unknown_order_raises(gw: MockGateway):
    with pytest.raises(OrderNotFound):
        gw.amend_order(BTC, "999", price=Decimal("1"))


def test_cancel_order(gw: MockGateway):
    order = gw.place_order(OrderRequest(contract=BTC, size=Decimal(1), price=Decimal("49000")))
    cancelled = gw.cancel_order(BTC, order.id)
    assert cancelled.status == "finished"
    assert cancelled.finish_as == "cancelled"
    assert gw.list_orders(BTC, "open") == []


def test_cancel_finished_order_raises(gw: MockGateway):
    order = gw.place_order(OrderRequest(contract=BTC, size=Decimal(1), price=Decimal("49000")))
    gw.cancel_order(BTC, order.id)
    with pytest.raises(OrderNotFound):
        gw.cancel_order(BTC, order.id)


# ---------- K 线 / 杠杆 ----------


def test_candlesticks_limit_and_range_exclusive(gw: MockGateway):
    gw.candles = [
        Candle(t=t, o=Decimal(1), h=Decimal(1), l=Decimal(1), c=Decimal(1), v=Decimal(1))
        for t in (100, 200, 300)
    ]
    assert len(gw.get_candlesticks(BTC, limit=2)) == 2
    assert [c.t for c in gw.get_candlesticks(BTC, from_ts=150, to_ts=300)] == [200, 300]
    with pytest.raises(ValueError, match="互斥"):
        gw.get_candlesticks(BTC, limit=2, from_ts=100)


def test_set_leverage(gw: MockGateway):
    pos = gw.set_leverage(BTC, 5, margin_mode="cross")
    assert pos.leverage == Decimal(5)
    with pytest.raises(ValueError, match="margin_mode"):
        gw.set_leverage(BTC, 5, margin_mode="bad")


# ---------- gate_rest 纯业务函数 ----------


def test_gen_client_order_id_format():
    text = gen_client_order_id()
    assert text.startswith("t-")
    assert len(text.encode()) == 28  # t- + 26 位，不超过 28 字节上限


def test_build_payload_market_order():
    payload = build_order_payload(OrderRequest(contract=BTC, size=Decimal(-2)))
    assert payload["price"] == "0"
    assert payload["tif"] == "ioc"
    assert payload["size"] == "-2"
    assert payload["reduce_only"] is False
    assert payload["text"].startswith("t-")


def test_build_payload_limit_order():
    req = OrderRequest(
        contract=BTC,
        size=Decimal(1),
        price=Decimal("49999.5"),
        tif="poc",
        reduce_only=True,
        stop_loss_price=Decimal("49000"),
        take_profit_price=Decimal("51000"),
        text="t-custom",
    )
    payload = build_order_payload(req)
    assert payload["price"] == "49999.5"
    assert payload["tif"] == "poc"
    assert payload["reduce_only"] is True
    assert payload["tpsl_sl_trigger_price"] == "49000"
    assert payload["tpsl_tp_trigger_price"] == "51000"
    assert payload["text"] == "t-custom"


def test_build_payload_close_order():
    payload = build_order_payload(OrderRequest(contract=BTC, close=True))
    assert payload["size"] == "0"
    assert payload["close"] is True
    assert payload["price"] == "0"
    assert payload["tif"] == "ioc"


# ---------- build_order_payload 自带 text 校验（Gate 自定义 ID 规则） ----------


def test_build_payload_custom_text_valid():
    req = OrderRequest(contract=BTC, size=Decimal(1), text="t-abc_DEF-123")
    assert build_order_payload(req)["text"] == "t-abc_DEF-123"
    boundary = OrderRequest(contract=BTC, size=Decimal(1), text="t-" + "a" * 26)  # 恰好 28 字节
    assert build_order_payload(boundary)["text"] == "t-" + "a" * 26


def test_build_payload_text_missing_prefix():
    with pytest.raises(ValueError, match="t-"):
        build_order_payload(OrderRequest(contract=BTC, size=Decimal(1), text="abc-def"))


def test_build_payload_text_too_long():
    with pytest.raises(ValueError, match="28"):
        build_order_payload(OrderRequest(contract=BTC, size=Decimal(1), text="t-" + "a" * 27))


def test_build_payload_text_invalid_charset():
    with pytest.raises(ValueError, match="字符"):
        build_order_payload(OrderRequest(contract=BTC, size=Decimal(1), text="t-abc.def"))


def test_wrap_gate_exception_labels():
    from gate_api.exceptions import ApiException

    def make(label: str) -> GateApiException:
        exp = ApiException(status=400, reason="Bad Request")
        return GateApiException(label=label, message="msg", exp=exp)

    not_found = wrap_gate_exception(make("ORDER_NOT_FOUND"))
    assert isinstance(not_found, OrderNotFound)
    assert not_found.label == "ORDER_NOT_FOUND"
    assert not_found.status == 400
    unknown = wrap_gate_exception(make("SOME_NEW_LABEL"))
    assert type(unknown) is GatewayError
