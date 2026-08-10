"""GateRestGateway 单元测试：monkeypatch mock 掉 gate_api.FuturesApi 方法，不触网。

重点覆盖下单超时回查语义（防重单）：
- 首次调用网络异常 → 按 text 回查
- 回查到订单 → 返回该订单
- 回查确认未创建（ORDER_NOT_FOUND）→ ORDER_TIMEOUT_NOT_CREATED，可安全重试
- 回查本身失败（含网络层异常）→ OrderStateUnknown，禁止盲目重试
"""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from gate_api.exceptions import ApiException, GateApiException

from src.config import GateConfig
from src.gateway import GatewayError, OrderRequest, OrderStateUnknown
from src.gateway.gate_rest import GateRestGateway

BTC = "BTC_USDT"


def make_gateway() -> GateRestGateway:
    """构造真实网关（仅初始化 SDK 客户端，不触网）。"""
    return GateRestGateway(GateConfig())


def make_sdk_order(order_id: str = "12345") -> SimpleNamespace:
    """模拟 SDK 返回的 FuturesOrder（仅 _to_order 读取的字段）。"""
    return SimpleNamespace(
        id=order_id,
        contract=BTC,
        status="finished",
        size="-2",
        price="59000",
        tif="gtc",
        is_reduce_only=True,
        left="0",
        fill_price="50000",
        finish_as="filled",
        text="t-test",
        tpsl_sl_trigger_price="",
        tpsl_tp_trigger_price="0",
    )


def make_gate_exc(label: str) -> GateApiException:
    """构造带 label 的 GateApiException（同 test_gateway_mock 的构造方式）。"""
    exp = ApiException(status=400, reason="Bad Request")
    return GateApiException(label=label, message="msg", exp=exp)


@pytest.fixture
def gw(monkeypatch: pytest.MonkeyPatch) -> GateRestGateway:
    """首次下单调用固定抛网络层异常（模拟超时）。"""
    gateway = make_gateway()
    monkeypatch.setattr(
        gateway._api, "create_futures_order", Mock(side_effect=ConnectionError("下单请求超时"))
    )
    return gateway


def test_place_order_timeout_recheck_finds_order(
    gw: GateRestGateway, monkeypatch: pytest.MonkeyPatch
):
    """首次超时、回查返回订单 → 返回该订单（不重单）。"""
    monkeypatch.setattr(gw._api, "get_futures_order", Mock(return_value=make_sdk_order()))
    result = gw.place_order(OrderRequest(contract=BTC, size=Decimal(1)))
    assert result.id == "12345"
    assert result.status == "finished"


def test_place_order_timeout_recheck_not_created(
    gw: GateRestGateway, monkeypatch: pytest.MonkeyPatch
):
    """首次超时、回查抛 ORDER_NOT_FOUND → 可安全重试语义。"""
    monkeypatch.setattr(
        gw._api, "get_futures_order", Mock(side_effect=make_gate_exc("ORDER_NOT_FOUND"))
    )
    with pytest.raises(GatewayError) as excinfo:
        gw.place_order(OrderRequest(contract=BTC, size=Decimal(1)))
    assert excinfo.value.label == "ORDER_TIMEOUT_NOT_CREATED"
    assert not isinstance(excinfo.value, OrderStateUnknown)


def test_place_order_timeout_recheck_network_error_unknown(
    gw: GateRestGateway, monkeypatch: pytest.MonkeyPatch
):
    """首次超时、回查遇网络层异常（非 GateApiException）→ OrderStateUnknown。"""
    monkeypatch.setattr(gw._api, "get_futures_order", Mock(side_effect=ConnectionError("回查断连")))
    with pytest.raises(OrderStateUnknown) as excinfo:
        gw.place_order(OrderRequest(contract=BTC, size=Decimal(1)))
    assert excinfo.value.label == "ORDER_STATE_UNKNOWN"


def test_place_order_timeout_recheck_other_gate_error_unknown(
    gw: GateRestGateway, monkeypatch: pytest.MonkeyPatch
):
    """首次超时、回查抛非 ORDER_NOT_FOUND 的 GateApiException → OrderStateUnknown。"""
    monkeypatch.setattr(
        gw._api, "get_futures_order", Mock(side_effect=make_gate_exc("CONTRACT_NOT_FOUND"))
    )
    with pytest.raises(OrderStateUnknown) as excinfo:
        gw.place_order(OrderRequest(contract=BTC, size=Decimal(1)))
    assert excinfo.value.label == "ORDER_STATE_UNKNOWN"


def test_place_order_gate_reject_no_recheck(monkeypatch: pytest.MonkeyPatch):
    """首次调用被服务端明确拒绝（GateApiException）→ 直接包装抛出，不回查。"""
    gateway = make_gateway()
    monkeypatch.setattr(
        gateway._api, "create_futures_order", Mock(side_effect=make_gate_exc("INVALID_PARAM"))
    )
    recheck = Mock()
    monkeypatch.setattr(gateway._api, "get_futures_order", recheck)
    with pytest.raises(GatewayError) as excinfo:
        gateway.place_order(OrderRequest(contract=BTC, size=Decimal(1)))
    assert excinfo.value.label == "INVALID_PARAM"
    recheck.assert_not_called()


def test_list_open_orders_supports_all_contracts_pagination_and_snapshot_fields(
    monkeypatch: pytest.MonkeyPatch,
):
    gateway = make_gateway()
    list_orders = Mock(return_value=[make_sdk_order("open-1")])
    monkeypatch.setattr(gateway._api, "list_futures_orders", list_orders)

    [order] = gateway.list_orders(limit=100, offset=200)

    list_orders.assert_called_once_with(gateway._settle, "open", offset=200, limit=100)
    assert order.id == "open-1"
    assert order.size == Decimal("-2")
    assert order.price == Decimal("59000")
    assert order.tif == "gtc"
    assert order.reduce_only is True
    assert order.stop_loss_price is None
    assert order.take_profit_price is None


def test_list_open_orders_maps_attached_tpsl_prices(monkeypatch: pytest.MonkeyPatch):
    gateway = make_gateway()
    sdk_order = make_sdk_order("protected")
    sdk_order.tpsl_sl_trigger_price = "58000"
    sdk_order.tpsl_tp_trigger_price = "62000"
    monkeypatch.setattr(gateway._api, "list_futures_orders", Mock(return_value=[sdk_order]))

    [order] = gateway.list_orders()

    assert order.stop_loss_price == Decimal("58000")
    assert order.take_profit_price == Decimal("62000")


def make_sdk_position() -> SimpleNamespace:
    """模拟 SDK 返回的 Position（仅 _to_position 读取的字段）。"""
    return SimpleNamespace(
        contract=BTC,
        size="0",
        entry_price="0",
        mark_price="50000",
        liq_price="0",
        leverage="2",
        margin="0",
        unrealised_pnl="0",
    )


def test_set_leverage_no_unsupported_kwargs(monkeypatch: pytest.MonkeyPatch):
    """当前 SDK 的 update_contract_position_leverage 不接受 x_gate_exptime。

    用严格签名（无 **kwargs）的 stub：若实现多传任何关键字参数会立刻 TypeError。
    """

    def strict_leverage_api(settle: str, contract: str, leverage: str, margin_mode: str):
        assert margin_mode in ("isolated", "cross")
        return make_sdk_position()

    gateway = make_gateway()
    monkeypatch.setattr(gateway._api, "update_contract_position_leverage", strict_leverage_api)
    pos = gateway.set_leverage(BTC, 2, "isolated")
    assert pos.contract == BTC
    assert pos.leverage == Decimal("2")


# ---------- 成交回报对账 REST：list_my_trades / list_position_close ----------


def make_sdk_my_trade() -> SimpleNamespace:
    """模拟 SDK 返回的 MyFuturesTrade（仅 _to_exchange_trade 读取的字段）。"""
    return SimpleNamespace(
        id=987,
        order_id=12345,
        contract=BTC,
        size="-2",
        price="50000.5",
        fee="0.05",
        role="taker",
        text="t-test",
        create_time=1700.25,
    )


def test_list_my_trades_maps_fields_and_default_args(monkeypatch: pytest.MonkeyPatch):
    gateway = make_gateway()
    get_my_trades = Mock(return_value=[make_sdk_my_trade()])
    monkeypatch.setattr(gateway._api, "get_my_trades", get_my_trades)

    [trade] = gateway.list_my_trades()

    get_my_trades.assert_called_once_with(
        gateway._settle, limit=100, _request_timeout=10
    )  # 无 contract 不传该参数
    assert trade.id == "987" and trade.order_id == "12345"  # id 归一为字符串
    assert trade.size == Decimal("-2") and trade.price == Decimal("50000.5")
    assert trade.fee == Decimal("0.05") and trade.role == "taker"
    assert trade.create_time == 1700.25


def test_list_my_trades_with_contract_and_wraps_error(monkeypatch: pytest.MonkeyPatch):
    gateway = make_gateway()
    get_my_trades = Mock(return_value=[])
    monkeypatch.setattr(gateway._api, "get_my_trades", get_my_trades)
    assert gateway.list_my_trades(BTC, limit=50) == []
    get_my_trades.assert_called_once_with(
        gateway._settle, limit=50, contract=BTC, _request_timeout=10
    )

    monkeypatch.setattr(
        gateway._api, "get_my_trades", Mock(side_effect=make_gate_exc("INVALID_PARAM"))
    )
    with pytest.raises(GatewayError) as excinfo:
        gateway.list_my_trades()
    assert excinfo.value.label == "INVALID_PARAM"


def make_sdk_position_close() -> SimpleNamespace:
    """模拟 SDK 返回的 PositionClose（仅 _to_position_close_record 读取的字段）。"""
    return SimpleNamespace(
        time=1700.75,
        contract=BTC,
        side="long",
        pnl="-3.5",
        pnl_pnl="-3.4",
        pnl_fee="-0.1",
        text="t-close",
        accum_size="2",
        first_open_time=1600.0,
    )


def test_list_position_close_maps_fields_and_int_window(monkeypatch: pytest.MonkeyPatch):
    gateway = make_gateway()
    list_position_close = Mock(return_value=[make_sdk_position_close()])
    monkeypatch.setattr(gateway._api, "list_position_close", list_position_close)

    [record] = gateway.list_position_close(BTC, 1600.9, 1701.1)

    # _from/to 取整（SDK 要求 int 秒）；带超时（悬挂会卡死启动/泄漏回填任务）
    list_position_close.assert_called_once_with(
        gateway._settle, contract=BTC, _from=1600, to=1701, _request_timeout=10
    )
    assert record.time == 1700.75 and record.contract == BTC
    assert record.pnl == Decimal("-3.5") and record.accum_size == Decimal("2")
    assert record.text == "t-close"


def test_list_position_close_wraps_error(monkeypatch: pytest.MonkeyPatch):
    gateway = make_gateway()
    monkeypatch.setattr(
        gateway._api, "list_position_close", Mock(side_effect=make_gate_exc("INVALID_PARAM"))
    )
    with pytest.raises(GatewayError) as excinfo:
        gateway.list_position_close(BTC, 0.0, 1.0)
    assert excinfo.value.label == "INVALID_PARAM"


# ---------- 持仓量：fetch_open_interest（contract_stats） ----------


def test_fetch_open_interest_takes_latest_stat(monkeypatch: pytest.MonkeyPatch):
    """按 time 取最新一条的 open_interest（str -> Decimal），不依赖响应排序。"""
    gateway = make_gateway()
    list_stats = Mock(
        return_value=[
            SimpleNamespace(time=100, open_interest="111"),
            SimpleNamespace(time=200, open_interest="999"),
        ]
    )
    monkeypatch.setattr(gateway._api, "list_contract_stats", list_stats)

    assert gateway.fetch_open_interest(BTC) == Decimal("999")
    list_stats.assert_called_once_with(gateway._settle, BTC, limit=1)


def test_fetch_open_interest_empty_returns_none(monkeypatch: pytest.MonkeyPatch):
    gateway = make_gateway()
    monkeypatch.setattr(gateway._api, "list_contract_stats", Mock(return_value=[]))
    assert gateway.fetch_open_interest(BTC) is None


def test_fetch_open_interest_wraps_error(monkeypatch: pytest.MonkeyPatch):
    gateway = make_gateway()
    monkeypatch.setattr(
        gateway._api, "list_contract_stats", Mock(side_effect=make_gate_exc("INVALID_PARAM"))
    )
    with pytest.raises(GatewayError) as excinfo:
        gateway.fetch_open_interest(BTC)
    assert excinfo.value.label == "INVALID_PARAM"


def test_fetch_open_interest_history_sorts_and_skips_incomplete(
    monkeypatch: pytest.MonkeyPatch,
):
    gateway = make_gateway()
    list_stats = Mock(
        return_value=[
            SimpleNamespace(time=200, open_interest="120"),
            SimpleNamespace(time=None, open_interest="999"),
            SimpleNamespace(time=100, open_interest="100"),
            SimpleNamespace(time=150, open_interest=None),
        ]
    )
    monkeypatch.setattr(gateway._api, "list_contract_stats", list_stats)

    points = gateway.fetch_open_interest_history(BTC, "4h", limit=3)

    assert [(point.time, point.value) for point in points] == [
        (100, Decimal("100")),
        (200, Decimal("120")),
    ]
    list_stats.assert_called_once_with(gateway._settle, BTC, interval="4h", limit=3)


def test_fetch_open_interest_history_wraps_error(monkeypatch: pytest.MonkeyPatch):
    gateway = make_gateway()
    monkeypatch.setattr(
        gateway._api, "list_contract_stats", Mock(side_effect=make_gate_exc("INVALID_PARAM"))
    )
    with pytest.raises(GatewayError) as excinfo:
        gateway.fetch_open_interest_history(BTC, "4h")
    assert excinfo.value.label == "INVALID_PARAM"
