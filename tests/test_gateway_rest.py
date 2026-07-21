"""GateRestGateway 单元测试：monkeypatch mock 掉 gate_api.FuturesApi 方法，不触网。

重点覆盖下单超时回查语义（防重单，已确认缺陷 P1-#11）：
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
    """回归：SDK 的 update_contract_position_leverage 不接受 x_gate_exptime（testnet 实测）。

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
