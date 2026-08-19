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
from urllib3.exceptions import ConnectTimeoutError, MaxRetryError, ReadTimeoutError

import gate_api
from src.config import GateConfig
from src.gateway import GatewayError, GatewayTransportError, OrderRequest, OrderStateUnknown
from src.gateway.gate_rest import (
    _DEFAULT_REQUEST_TIMEOUT,
    GateRestGateway,
    _TimeoutApiClient,
    _to_position,
)

BTC = "BTC_USDT"


def make_gateway() -> GateRestGateway:
    """构造真实网关（仅初始化 SDK 客户端，不触网）。

    参数：无
    返回：
        GateRestGateway，返回该测试辅助函数构造或记录的结果
    """
    return GateRestGateway(GateConfig())


def make_sdk_order(order_id: str = "12345") -> SimpleNamespace:
    """模拟 SDK 返回的 FuturesOrder（仅 _to_order 读取的字段）。

    参数：
        order_id: str，订单标识
    返回：
        SimpleNamespace，返回该测试辅助函数构造或记录的结果
    """
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
    """构造带 label 的 GateApiException（同 test_gateway_mock 的构造方式）。

    参数：
        label: str，Gate 异常标签
    返回：
        GateApiException，返回该测试辅助函数构造或记录的结果
    """
    exp = ApiException(status=400, reason="Bad Request")
    return GateApiException(label=label, message="msg", exp=exp)


@pytest.fixture
def gw(monkeypatch: pytest.MonkeyPatch) -> GateRestGateway:
    """首次下单调用固定抛网络层异常（模拟超时）。

    参数：
        monkeypatch: pytest.MonkeyPatch，pytest 运行时替换夹具
    返回：
        GateRestGateway，返回该测试辅助函数构造或记录的结果
    """
    gateway = make_gateway()
    monkeypatch.setattr(
        gateway._api, "create_futures_order", Mock(side_effect=ConnectionError("下单请求超时"))
    )
    return gateway


def test_place_order_timeout_recheck_finds_order(
    gw: GateRestGateway, monkeypatch: pytest.MonkeyPatch
):
    """首次超时、回查返回订单 → 返回该订单（不重单）。

    参数：
        gw: GateRestGateway，模拟交易或 Gate 网关
        monkeypatch: pytest.MonkeyPatch，pytest 运行时替换夹具
    返回：
        None，执行断言验证目标行为
    """
    monkeypatch.setattr(gw._api, "get_futures_order", Mock(return_value=make_sdk_order()))
    result = gw.place_order(OrderRequest(contract=BTC, size=Decimal(1)))
    assert result.id == "12345"
    assert result.status == "finished"


def test_place_order_timeout_recheck_not_created(
    gw: GateRestGateway, monkeypatch: pytest.MonkeyPatch
):
    """首次超时、回查抛 ORDER_NOT_FOUND → 可安全重试语义。

    参数：
        gw: GateRestGateway，模拟交易或 Gate 网关
        monkeypatch: pytest.MonkeyPatch，pytest 运行时替换夹具
    返回：
        None，执行断言验证目标行为
    """
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
    """首次超时、回查遇网络层异常（非 GateApiException）→ OrderStateUnknown。

    参数：
        gw: GateRestGateway，模拟交易或 Gate 网关
        monkeypatch: pytest.MonkeyPatch，pytest 运行时替换夹具
    返回：
        None，执行断言验证目标行为
    """
    monkeypatch.setattr(gw._api, "get_futures_order", Mock(side_effect=ConnectionError("回查断连")))
    with pytest.raises(OrderStateUnknown) as excinfo:
        gw.place_order(OrderRequest(contract=BTC, size=Decimal(1)))
    assert excinfo.value.label == "ORDER_STATE_UNKNOWN"


def test_place_order_timeout_recheck_other_gate_error_unknown(
    gw: GateRestGateway, monkeypatch: pytest.MonkeyPatch
):
    """首次超时、回查抛非 ORDER_NOT_FOUND 的 GateApiException → OrderStateUnknown。

    参数：
        gw: GateRestGateway，模拟交易或 Gate 网关
        monkeypatch: pytest.MonkeyPatch，pytest 运行时替换夹具
    返回：
        None，执行断言验证目标行为
    """
    monkeypatch.setattr(
        gw._api, "get_futures_order", Mock(side_effect=make_gate_exc("CONTRACT_NOT_FOUND"))
    )
    with pytest.raises(OrderStateUnknown) as excinfo:
        gw.place_order(OrderRequest(contract=BTC, size=Decimal(1)))
    assert excinfo.value.label == "ORDER_STATE_UNKNOWN"


def test_place_order_gate_reject_no_recheck(monkeypatch: pytest.MonkeyPatch):
    """首次调用被服务端明确拒绝（GateApiException）→ 直接包装抛出，不回查。

    参数：
        monkeypatch: pytest.MonkeyPatch，pytest 运行时替换夹具
    返回：
        None，执行断言验证目标行为
    """
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
    """校验全部合约挂单查询的分页参数透传与订单快照字段映射。

    参数：
        monkeypatch: pytest.MonkeyPatch，pytest 运行时替换夹具
    返回：
        None，执行断言验证目标行为
    """
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
    """校验挂单附带止盈止损触发价映射为订单的止损/止盈价字段。

    参数：
        monkeypatch: pytest.MonkeyPatch，pytest 运行时替换夹具
    返回：
        None，执行断言验证目标行为
    """
    gateway = make_gateway()
    sdk_order = make_sdk_order("protected")
    sdk_order.tpsl_sl_trigger_price = "58000"
    sdk_order.tpsl_tp_trigger_price = "62000"
    monkeypatch.setattr(gateway._api, "list_futures_orders", Mock(return_value=[sdk_order]))

    [order] = gateway.list_orders()

    assert order.stop_loss_price == Decimal("58000")
    assert order.take_profit_price == Decimal("62000")


def make_sdk_position() -> SimpleNamespace:
    """模拟 SDK 返回的 Position（仅 _to_position 读取的字段）。

    参数：无
    返回：
        SimpleNamespace，返回该测试辅助函数构造或记录的结果
    """
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


def test_to_position_cross_prefers_config_limit():
    """全仓实际杠杆以配置值 cross_leverage_limit 为锚点，不被有效杠杆 lever 覆盖。

    参数：无
    返回：
        None，断言模式为 cross、杠杆锚点取 5（而非 lever 的 4.35）、leverage 归 0
    """
    pos = make_sdk_position()
    pos.leverage = "0"
    pos.pos_margin_mode = "cross"
    pos.cross_leverage_limit = "5"
    pos.lever = "4.35"
    p = _to_position(pos)
    assert p.margin_mode == "cross"
    assert p.cross_leverage_limit == Decimal(5)
    assert p.leverage == Decimal(0)


def test_to_position_cross_falls_back_to_lever():
    """cross_leverage_limit 缺失或为 0 时回退 lever 作为全仓实际杠杆。

    参数：无
    返回：
        None，断言空串回退 lever=7、lever 为 "0" 时视为缺失再回退配置值 6
    """
    pos = make_sdk_position()
    pos.leverage = "0"
    pos.pos_margin_mode = "cross"
    pos.cross_leverage_limit = ""
    pos.lever = "7"
    assert _to_position(pos).cross_leverage_limit == Decimal(7)
    pos.cross_leverage_limit = "6"
    pos.lever = "0"
    assert _to_position(pos).cross_leverage_limit == Decimal(6)


def test_to_position_cross_unknown_when_both_missing():
    """cross_leverage_limit 与 lever 均缺失时全仓实际杠杆为 None（不可信）。

    参数：无
    返回：
        None，断言 cross_leverage_limit 为 None
    """
    pos = make_sdk_position()
    pos.leverage = "0"
    pos.pos_margin_mode = "cross"
    pos.cross_leverage_limit = None
    pos.lever = None
    assert _to_position(pos).cross_leverage_limit is None


def test_to_position_infers_mode_without_pos_margin_mode():
    """旧 SDK 缺 pos_margin_mode 属性时按 leverage==0 推断全仓（getattr 防御）。

    参数：无
    返回：
        None，断言 leverage="0" 推断为 cross 并取配置值 6、leverage="2" 为逐仓
    """
    pos = make_sdk_position()  # 无 pos_margin_mode/lever 属性
    pos.leverage = "0"
    pos.cross_leverage_limit = "6"
    p = _to_position(pos)
    assert p.margin_mode == "cross" and p.cross_leverage_limit == Decimal(6)
    pos2 = make_sdk_position()
    p2 = _to_position(pos2)
    assert p2.margin_mode == "isolated" and p2.cross_leverage_limit is None
    assert p2.leverage == Decimal(2)


def test_to_position_isolated_prefers_lever():
    """逐仓持仓优先取新协议 lever 字段（旧 leverage 可能为 0），避免真实杠杆被快照成 1x。

    参数：无
    返回：
        None，断言 isolated + leverage="0" + lever="30" 映射杠杆为 30、lever 缺失回退旧字段
    """
    pos = make_sdk_position()
    pos.pos_margin_mode = "isolated"
    pos.leverage = "0"
    pos.lever = "30"
    p = _to_position(pos)
    assert p.margin_mode == "isolated" and p.leverage == Decimal(30)
    pos.lever = None  # lever 缺失时回退旧 leverage 字段
    assert _to_position(pos).leverage == Decimal(0)


def test_set_leverage_no_unsupported_kwargs(monkeypatch: pytest.MonkeyPatch):
    """当前 SDK 的 update_contract_position_leverage 不接受 x_gate_exptime。

    参数：
        monkeypatch: pytest.MonkeyPatch，pytest 运行时替换夹具
    返回：
        None，执行断言验证目标行为
    """

    def strict_leverage_api(settle: str, contract: str, leverage: str, margin_mode: str):
        """严格签名（无 **kwargs）的杠杆设置桩：实现多传任何关键字参数会立刻 TypeError。

        参数：
        settle: str，结算币种
            contract: str，合约标识
        leverage: str，目标杠杆倍数
        margin_mode: str，保证金模式
        返回：
        SimpleNamespace，模拟 SDK 返回的持仓对象
        """
        assert margin_mode in ("isolated", "cross")
        return make_sdk_position()

    gateway = make_gateway()
    monkeypatch.setattr(gateway._api, "update_contract_position_leverage", strict_leverage_api)
    pos = gateway.set_leverage(BTC, 2, "isolated")
    assert pos.contract == BTC
    assert pos.leverage == Decimal("2")


# ---------- 成交回报对账 REST：list_my_trades / list_position_close ----------


def make_sdk_my_trade() -> SimpleNamespace:
    """模拟 SDK 返回的 MyFuturesTrade（仅 _to_exchange_trade 读取的字段）。

    参数：无
    返回：
        SimpleNamespace，返回该测试辅助函数构造或记录的结果
    """
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
    """校验我的成交查询的默认参数与成交字段映射。

    参数：
        monkeypatch: pytest.MonkeyPatch，pytest 运行时替换夹具
    返回：
        None，执行断言验证目标行为
    """
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
    """校验带合约过滤的我的成交查询参数透传，以及 SDK 异常的包装。

    参数：
        monkeypatch: pytest.MonkeyPatch，pytest 运行时替换夹具
    返回：
        None，执行断言验证目标行为
    """
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
    """模拟 SDK 返回的 PositionClose（仅 _to_position_close_record 读取的字段）。

    参数：无
    返回：
        SimpleNamespace，返回该测试辅助函数构造或记录的结果
    """
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
    """验证平仓记录查询会映射字段并使用整数时间窗口。

    参数：
        monkeypatch: pytest.MonkeyPatch，pytest 运行时替换夹具
    返回：
        None，执行断言验证目标行为
    """
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
    """验证平仓记录查询会将 SDK 异常包装为网关异常。

    参数：
        monkeypatch: pytest.MonkeyPatch，pytest 运行时替换夹具
    返回：
        None，执行断言验证目标行为
    """
    gateway = make_gateway()
    monkeypatch.setattr(
        gateway._api, "list_position_close", Mock(side_effect=make_gate_exc("INVALID_PARAM"))
    )
    with pytest.raises(GatewayError) as excinfo:
        gateway.list_position_close(BTC, 0.0, 1.0)
    assert excinfo.value.label == "INVALID_PARAM"


# ---------- 持仓量：fetch_open_interest（contract_stats） ----------


def test_fetch_open_interest_takes_latest_stat(monkeypatch: pytest.MonkeyPatch):
    """按 time 取最新一条的 open_interest（str -> Decimal），不依赖响应排序。

    参数：
        monkeypatch: pytest.MonkeyPatch，pytest 运行时替换夹具
    返回：
        None，执行断言验证目标行为
    """
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
    """验证持仓量接口返回空数据时结果为 None。

    参数：
        monkeypatch: pytest.MonkeyPatch，pytest 运行时替换夹具
    返回：
        None，执行断言验证目标行为
    """
    gateway = make_gateway()
    monkeypatch.setattr(gateway._api, "list_contract_stats", Mock(return_value=[]))
    assert gateway.fetch_open_interest(BTC) is None


def test_fetch_open_interest_wraps_error(monkeypatch: pytest.MonkeyPatch):
    """验证持仓量查询会将 SDK 异常包装为网关异常。

    参数：
        monkeypatch: pytest.MonkeyPatch，pytest 运行时替换夹具
    返回：
        None，执行断言验证目标行为
    """
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
    """验证持仓量历史会按时间排序并跳过字段不完整的记录。

    参数：
        monkeypatch: pytest.MonkeyPatch，pytest 运行时替换夹具
    返回：
        None，执行断言验证目标行为
    """
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
    """验证持仓量历史查询会将 SDK 异常包装为网关异常。

    参数：
        monkeypatch: pytest.MonkeyPatch，pytest 运行时替换夹具
    返回：
        None，执行断言验证目标行为
    """
    gateway = make_gateway()
    monkeypatch.setattr(
        gateway._api, "list_contract_stats", Mock(side_effect=make_gate_exc("INVALID_PARAM"))
    )
    with pytest.raises(GatewayError) as excinfo:
        gateway.fetch_open_interest_history(BTC, "4h")
    assert excinfo.value.label == "INVALID_PARAM"


def test_gateway_uses_timeout_api_client():
    """验证真实网关默认装配带超时注入的 ApiClient（读路径不再不限时悬挂）。

    参数：无
    返回：
        None，断言网关内部 SDK 客户端为 _TimeoutApiClient 实例
    """
    gateway = make_gateway()
    assert isinstance(gateway._api.api_client, _TimeoutApiClient)


def test_timeout_client_injects_default_timeout(monkeypatch: pytest.MonkeyPatch):
    """验证未显式指定 _request_timeout 的调用被注入默认（连接, 读取）超时。

    参数：
        monkeypatch: pytest.MonkeyPatch，替换父类 call_api 以捕获透传参数
    返回：
        None，断言缺省与显式 None 两种形态都被替换为默认超时元组
    """
    captured: dict = {}

    def fake_call_api(self, *args, **kwargs):
        """记录透传参数的伪 call_api（不触网）。

        参数：
            args: tuple，位置参数
            kwargs: dict，关键字参数
        返回：
            None，仅记录参数
        """
        captured.update(kwargs)

    monkeypatch.setattr(gate_api.ApiClient, "call_api", fake_call_api)
    client = _TimeoutApiClient(gate_api.Configuration(host="http://localhost"))
    client.call_api("/futures/usdt/positions", "GET")
    assert captured["_request_timeout"] == _DEFAULT_REQUEST_TIMEOUT
    client.call_api("/futures/usdt/positions", "GET", _request_timeout=None)
    assert captured["_request_timeout"] == _DEFAULT_REQUEST_TIMEOUT


def test_timeout_client_preserves_explicit_timeout(monkeypatch: pytest.MonkeyPatch):
    """验证调用方显式指定的 _request_timeout（如下单 10s）不被默认值覆盖。

    参数：
        monkeypatch: pytest.MonkeyPatch，替换父类 call_api 以捕获透传参数
    返回：
        None，断言显式超时值原样透传
    """
    captured: dict = {}

    def fake_call_api(self, *args, **kwargs):
        """记录透传参数的伪 call_api（不触网）。

        参数：
            args: tuple，位置参数
            kwargs: dict，关键字参数
        返回：
            None，仅记录参数
        """
        captured.update(kwargs)

    monkeypatch.setattr(gate_api.ApiClient, "call_api", fake_call_api)
    client = _TimeoutApiClient(gate_api.Configuration(host="http://localhost"))
    client.call_api("/futures/usdt/orders", "POST", _request_timeout=10)
    assert captured["_request_timeout"] == 10


# ---------- PR #84 第二轮评审回归：传输层异常归一化与写操作结果未知 ----------


@pytest.mark.parametrize(
    "transport_exc",
    [
        ReadTimeoutError(None, "/", "read timed out"),
        ConnectTimeoutError(None, "/", "connect timed out"),
        MaxRetryError(None, "/", "too many errors"),
    ],
)
def test_transport_exceptions_normalized(transport_exc, monkeypatch: pytest.MonkeyPatch):
    """验证 urllib3 传输层异常（读超时/连接超时/重试耗尽）归一化为 GatewayTransportError。

    参数：
        transport_exc: Exception，参数化的 urllib3 传输层异常实例
        monkeypatch: pytest.MonkeyPatch，替换父类 call_api 抛出传输层异常
    返回：
        None，断言读接口抛出 label=TRANSPORT_UNKNOWN 的 GatewayTransportError，
        且为 GatewayError 子类（只读路由自动 502、agent 错误契约稳定）
    """
    gateway = make_gateway()
    monkeypatch.setattr(gate_api.ApiClient, "call_api", Mock(side_effect=transport_exc))
    with pytest.raises(GatewayTransportError) as excinfo:
        gateway.list_positions()
    assert excinfo.value.label == "TRANSPORT_UNKNOWN"
    assert isinstance(excinfo.value, GatewayError)


def test_gate_api_exception_not_wrapped_as_transport(monkeypatch: pytest.MonkeyPatch):
    """验证 GateApiException（服务端明确拒绝）不被传输层归一化误包。

    参数：
        monkeypatch: pytest.MonkeyPatch，替换父类 call_api 抛出 GateApiException
    返回：
        None，断言读接口抛出按 label 映射的普通 GatewayError 而非 GatewayTransportError
    """
    gateway = make_gateway()
    monkeypatch.setattr(
        gate_api.ApiClient, "call_api", Mock(side_effect=make_gate_exc("SOME_LABEL"))
    )
    with pytest.raises(GatewayError) as excinfo:
        gateway.list_positions()
    assert not isinstance(excinfo.value, GatewayTransportError)
    assert excinfo.value.label == "SOME_LABEL"


def test_amend_order_transport_timeout_state_unknown(monkeypatch: pytest.MonkeyPatch):
    """验证改单传输超时映射为 OrderStateUnknown（交易所可能已执行，禁止盲目重试）。

    参数：
        monkeypatch: pytest.MonkeyPatch，替换父类 call_api 抛出读超时
    返回：
        None，断言 amend_order 抛出 label=ORDER_STATE_UNKNOWN 的 OrderStateUnknown
    """
    gateway = make_gateway()
    monkeypatch.setattr(
        gate_api.ApiClient,
        "call_api",
        Mock(side_effect=ReadTimeoutError(None, "/", "read timed out")),
    )
    with pytest.raises(OrderStateUnknown) as excinfo:
        gateway.amend_order(BTC, "12345", price=Decimal("59000"))
    assert excinfo.value.label == "ORDER_STATE_UNKNOWN"


def test_cancel_order_transport_timeout_state_unknown(monkeypatch: pytest.MonkeyPatch):
    """验证撤单传输超时映射为 OrderStateUnknown（交易所可能已执行，禁止盲目重试）。

    参数：
        monkeypatch: pytest.MonkeyPatch，替换父类 call_api 抛出连接超时
    返回：
        None，断言 cancel_order 抛出 label=ORDER_STATE_UNKNOWN 的 OrderStateUnknown
    """
    gateway = make_gateway()
    monkeypatch.setattr(
        gate_api.ApiClient,
        "call_api",
        Mock(side_effect=ConnectTimeoutError(None, "/", "connect timed out")),
    )
    with pytest.raises(OrderStateUnknown) as excinfo:
        gateway.cancel_order(BTC, "12345")
    assert excinfo.value.label == "ORDER_STATE_UNKNOWN"


def _raw_api_exception(status: int, reason: str, body: bytes | None = None) -> ApiException:
    """构造 SDK 未归一化的原始 ApiException（非 GateApiException 子类路径）。

    参数：
        status: int，HTTP 状态码（0 表示 SSL/连接层失败）
        reason: str，错误描述
        body: bytes | None，响应体；None 模拟 SSL 失败时无响应体

    返回：
        ApiException：不带 Gate 私有 label 的原始异常实例
    """
    exc = ApiException(status=status, reason=reason)
    exc.body = body
    return exc


_SDK_RAW_EXCEPTIONS = [
    _raw_api_exception(502, "Bad Gateway", b"<html>Bad Gateway</html>"),
    _raw_api_exception(0, "SSLError: certificate verify failed"),
    AttributeError("'NoneType' object has no attribute 'decode'"),
]


@pytest.mark.parametrize("sdk_exc", _SDK_RAW_EXCEPTIONS)
def test_sdk_raw_exceptions_normalized(sdk_exc: Exception, monkeypatch: pytest.MonkeyPatch):
    """验证 SDK 三类原始异常（无 label 非 2xx 响应/SSL 失败/空体 decode 崩溃）统一归一化。

    参数：
        sdk_exc: Exception，参数化的 SDK 原始异常实例
        monkeypatch: pytest.MonkeyPatch，替换父类 call_api 抛出该异常

    返回：
        None，断言读操作抛出 label=TRANSPORT_UNKNOWN 的 GatewayTransportError——
        不向上泄漏 ApiException/AttributeError 等 SDK 内部类型（PR #84 评审 P1）
    """
    gateway = make_gateway()
    monkeypatch.setattr(gate_api.ApiClient, "call_api", Mock(side_effect=sdk_exc))
    with pytest.raises(GatewayTransportError) as excinfo:
        gateway.list_positions()
    assert excinfo.value.label == "TRANSPORT_UNKNOWN"


@pytest.mark.parametrize("sdk_exc", _SDK_RAW_EXCEPTIONS)
def test_amend_cancel_sdk_raw_exceptions_state_unknown(
    sdk_exc: Exception, monkeypatch: pytest.MonkeyPatch
):
    """验证改单/撤单遭遇 SDK 三类原始异常时落 ORDER_STATE_UNKNOWN（禁止盲目重试）。

    参数：
        sdk_exc: Exception，参数化的 SDK 原始异常实例
        monkeypatch: pytest.MonkeyPatch，替换父类 call_api 抛出该异常

    返回：
        None，断言 amend_order/cancel_order 均抛 label=ORDER_STATE_UNKNOWN 的
        OrderStateUnknown（与传输超时同一 fail-closed 语义）
    """
    gateway = make_gateway()
    monkeypatch.setattr(gate_api.ApiClient, "call_api", Mock(side_effect=sdk_exc))
    with pytest.raises(OrderStateUnknown) as excinfo:
        gateway.amend_order(BTC, "12345", price=Decimal("59000"))
    assert excinfo.value.label == "ORDER_STATE_UNKNOWN"
    with pytest.raises(OrderStateUnknown) as excinfo2:
        gateway.cancel_order(BTC, "12345")
    assert excinfo2.value.label == "ORDER_STATE_UNKNOWN"


def test_pool_manager_request_patches_total_deadline():
    """验证 PoolManager 入口被包装为共享 deadline 重试层（连接/读取/整次三者齐备）。

    参数：无

    返回：
        None，断言 SDK tuple 路径（connect/read）被补 total=30、None 路径构造完整
        默认组合、显式 int 路径（下单 total=10）不被覆盖；网关 PoolManager.request
        已替换为共享 deadline 包装（PR #84 评审 P2）
    """
    import urllib3

    from src.gateway.gate_rest import _ensure_total_deadline

    assert _ensure_total_deadline(None).total == 30
    t = _ensure_total_deadline(urllib3.Timeout(connect=5, read=15))
    assert (t.connect_timeout, t.read_timeout, t.total) == (5, 15, 30)
    assert _ensure_total_deadline(urllib3.Timeout(total=10)).total == 10

    gateway = make_gateway()
    pool_manager = gateway._api.api_client.rest_client.pool_manager
    assert pool_manager.request.__name__ == "_request_with_total_deadline"


def test_shared_deadline_retries_succeed_after_two_timeouts():
    """验证重试共享同一 deadline：两次传输超时后第三次成功仍正常返回。

    参数：无

    返回：
        None，断言第三次尝试成功返回结果，且每次尝试都带收紧后的
        connect/read 超时与 retries=False（PR #84 评审 P2）
    """
    from src.gateway.gate_rest import _call_with_shared_deadline

    calls: list[dict] = []

    def _flaky(method, url, **kwargs):
        calls.append(kwargs)
        if len(calls) < 3:
            raise ReadTimeoutError(None, url, "read timed out")
        return "ok"

    result = _call_with_shared_deadline(_flaky, "GET", "https://example.com", {})
    assert result == "ok"
    assert len(calls) == 3
    assert all(c["retries"] is False for c in calls)
    assert all(isinstance(c["timeout"].connect_timeout, (int, float)) for c in calls)
    assert all(isinstance(c["timeout"].read_timeout, (int, float)) for c in calls)


def test_shared_deadline_clamps_attempt_timeouts_to_remaining_budget():
    """验证后续尝试的连接/读取超时按剩余预算收紧，预算耗尽不再发起新尝试。

    参数：无

    返回：
        None，断言 1s 共享预算下：第二次尝试的 read 被收紧到剩余约 0.2s、
        第三次尝试因预算耗尽未发起——重试不会像 urllib3 Retry 那样每次
        重新起表（PR #84 评审 P2）
    """
    import time

    import urllib3

    from src.gateway.gate_rest import _call_with_shared_deadline

    attempts: list[urllib3.Timeout] = []

    def _slow_fail(method, url, **kwargs):
        attempts.append(kwargs["timeout"])
        time.sleep(0.6)  # 每次尝试实际耗时 0.6s，快速耗尽共享预算
        raise ReadTimeoutError(None, url, "read timed out")

    with pytest.raises(ReadTimeoutError):
        _call_with_shared_deadline(
            _slow_fail, "GET", "https://example.com", {"timeout": urllib3.Timeout(total=1.0)}
        )
    assert len(attempts) == 2  # 第三次尝试因预算耗尽未发起
    assert 0.9 < attempts[0].read_timeout <= 1.0  # 首次：read 收紧到完整预算
    assert (
        0 < attempts[1].read_timeout < 0.45
    )  # 第二次：只剩约 0.2s 预算（0.6s 尝试 + 0.2s 退避后）


def test_shared_deadline_post_never_retried():
    """验证 POST 下单/改撤单类请求绝不重试（防重单），一次超时即原样上抛。

    参数：无

    返回：
        None，断言 POST 只发起一次尝试并抛出传输异常（PR #84 评审 P2）
    """
    from src.gateway.gate_rest import _call_with_shared_deadline

    calls = 0

    def _fail(method, url, **kwargs):
        nonlocal calls
        calls += 1
        raise ReadTimeoutError(None, url, "read timed out")

    with pytest.raises(ReadTimeoutError):
        _call_with_shared_deadline(_fail, "POST", "https://example.com", {})
    assert calls == 1
