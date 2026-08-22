"""PaperGateway 模拟撮合引擎测试：盈亏、手续费、资金费、强平、保证金占用/释放。"""

from decimal import Decimal

import pytest

from src.config import PaperConfig
from src.gateway.base import Contract, GatewayError, OrderNotFound, OrderRequest
from src.gateway.market_stats import OpenInterestPoint
from src.paper import PaperGateway

BTC = "BTC_USDT"
D = Decimal


def make_contract(taker: str = "0.0005", maker: str = "0.0002") -> Contract:
    """构造测试用 BTC_USDT 永续合约定义，标记价 100、资金费率 0.0001、乘数 1。

    参数：
        taker: str，taker 手续费率（十进制字符串）
        maker: str，maker 手续费率（十进制字符串）

    返回：
        Contract：状态为交易中的 BTC 永续合约静态定义
    """
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
    """构造初始权益 10000 的模拟网关，并喂入首口行情（mark 100 / bid 99.9 / ask 100.1）。

    参数：
        slippage: str，市价单滑点比例（十进制字符串）
        taker: str，taker 手续费率（十进制字符串）

    返回：
        PaperGateway：已注册 BTC 合约、维持保证金率 0.005 且可直接下单的模拟网关
    """
    cfg = PaperConfig(initial_equity=10000.0, slippage=float(slippage))
    gw = PaperGateway(cfg, contracts={BTC: make_contract(taker=taker)}, maintenance_rate=D("0.005"))
    gw.on_price(BTC, D("100"), D("99.9"), D("100.1"))
    return gw


def buy(gw: PaperGateway, size, price=None):
    """BTC 下单便捷封装：size 正数开多、负数开空，price 为 None 即市价单。

    参数：
        gw: PaperGateway，目标模拟网关
        size: 委托张数，正数买入开多、负数卖出开空
        price: 限价价格；None 表示市价单

    返回：
        OrderResult：下单结果，含成交状态与成交均价
    """
    return gw.place_order(OrderRequest(contract=BTC, size=D(size), price=price))


def close_all(gw: PaperGateway):
    """对 BTC 当前持仓下市价平仓单（close=True），一键整仓平仓。

    参数：
        gw: PaperGateway，目标模拟网关

    返回：
        OrderResult：平仓单结果，含平仓成交价与完成状态
    """
    return gw.place_order(OrderRequest(contract=BTC, close=True))


def test_long_profit():
    """验证做多盈利全链路算账：开仓占保证金与 taker 费、涨后浮盈、平仓释放保证金入已实现盈亏。

    参数：无

    返回：
        None，断言可用余额、浮动/已实现盈亏与平仓成交价全部符合手工算账结果
    """
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
    """验证做多亏损平仓：可用余额扣掉亏损与双边手续费，已实现盈亏为负。

    参数：无

    返回：
        None，断言平仓后可用余额为 9899.05、累计已实现盈亏为 -100
    """
    gw = make_gateway()
    buy(gw, 10)
    gw.on_price(BTC, D("90"), D("89.9"), D("90.1"))
    close_all(gw)
    assert gw.account.available == D("9899.05")  # -100 盈亏 - 0.5 - 0.45 费
    assert gw.account.total_realized == D("-100")


def test_short_profit():
    """验证做空下跌盈利：平仓后可用余额增加盈利并扣除双边手续费。

    参数：无

    返回：
        None，断言平仓后可用余额为 10099.05（+100 盈亏，双边费 0.95）
    """
    gw = make_gateway()
    buy(gw, -10)
    gw.on_price(BTC, D("90"), D("89.9"), D("90.1"))
    close_all(gw)
    assert gw.account.available == D("10099.05")  # +100 盈亏 - 0.5 - 0.45 费


def test_short_loss():
    """验证做空上涨亏损：平仓后可用余额扣掉亏损与双边手续费。

    参数：无

    返回：
        None，断言平仓后可用余额为 9898.95
    """
    gw = make_gateway()
    buy(gw, -10)
    gw.on_price(BTC, D("110"), D("109.9"), D("110.1"))
    close_all(gw)
    assert gw.account.available == D("9898.95")


def test_fee_taker_and_maker():
    """验证手续费按成交角色分别计费：挂单成交按 maker 费率，市价平仓按 taker 费率并累计。

    参数：无

    返回：
        None，断言 maker 成交费 0.198、平仓 taker 费 0.495，累计手续费为两者之和
    """
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
    """验证限价单价格穿越对手价时立即按对手价保守成交（记 taker），不吃自报价的更优价。

    参数：无

    返回：
        None，断言买单按 ask 100.1、卖单按 bid 99.9 立即成交且标记为非 maker
    """
    gw = make_gateway()
    result = buy(gw, 10, price=D("100.1"))  # ≥ ask → 立即成交，吃对手价而非更优价
    assert result.status == "finished" and result.fill_price == D("100.1")
    assert gw.account.fills[-1].maker is False
    result = buy(gw, -10, price=D("99.9"))  # ≤ bid → 立即成交于 bid
    assert result.fill_price == D("99.9")


def test_limit_ioc_cancelled_when_not_crossed():
    """验证 IOC 限价单未穿越对手价时立即撤销，不留挂单。

    参数：无

    返回：
        None，断言订单以 cancelled 结束且挂单列表为空
    """
    gw = make_gateway()
    result = gw.place_order(OrderRequest(contract=BTC, size=D(10), price=D("99"), tif="ioc"))
    assert result.status == "finished" and result.finish_as == "cancelled"
    assert gw.list_orders(BTC) == []


def test_limit_order_keeps_attached_stop_loss_without_take_profit():
    """验证仅挂止损价的限价单：下单结果与挂单快照都保留止损价，止盈价为 None。

    参数：无

    返回：
        None，断言返回结果与在挂订单的 stop_loss_price 为 95、take_profit_price 为 None
    """
    gw = make_gateway()
    result = gw.place_order(
        OrderRequest(
            contract=BTC,
            size=D(10),
            price=D("99"),
            stop_loss_price=D("95"),
        )
    )

    assert result.stop_loss_price == D("95")
    assert result.take_profit_price is None
    assert gw.list_orders(BTC)[0].stop_loss_price == D("95")


def test_market_slippage():
    """验证配置滑点后市价单成交价偏移：买单按标记价 ×(1+滑点)、卖单 ×(1-滑点)。

    参数：无

    返回：
        None，断言 0.001 滑点下买单成交于 100.1、卖单成交于 99.9
    """
    gw = make_gateway(slippage="0.001")
    result = buy(gw, 10)
    assert result.fill_price == D("100.1")  # mark × (1 + 0.001)
    result = buy(gw, -5)
    assert result.fill_price == D("99.9")  # mark × (1 - 0.001)


def test_funding_long_pays_short_receives():
    """验证正资金费率下多头支付、空头收取，翻仓后方向切换仍按新持仓方向结算。

    参数：无

    返回：
        None，断言两次资金费结算差额分别为 -1 与 +1，并正确计入可用余额与累计资金费
    """
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
    """验证负资金费率下多头收取资金费，零费率不产生结算。

    参数：无

    返回：
        None，断言负费率结算差额为 +1、零费率结算差额为 0
    """
    gw = make_gateway()
    buy(gw, 10)
    assert gw.settle_funding(BTC, D("-0.001")) == D("1")  # 负费率：多头收
    assert gw.settle_funding(BTC, D("0")) == D("0")


def test_liquidation():
    """验证高杠杆多仓跌穿维持保证金触发强平：仓位清零、保证金全损无返还、记录强平事件。

    参数：无

    返回：
        None，断言强平事件张数/亏损/返还保证金及强平后可用余额与持仓清空
    """
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


def test_liquidation_cancels_resting_orders_and_tpsl():
    """验证强平清场：残留挂单与 TPSL 一并撤销，后续行情不再自动重新开仓（issue #71）。

    参数：无

    返回：
        None，断言强平后该合约挂单/TPSL 清空，且下一 tick 不产生任何新开仓成交
    """
    from src.gateway.base import TpslOrder

    gw = make_gateway(taker="0")
    gw.set_leverage(BTC, 10)
    buy(gw, 10)  # 保证金 = 1000 / 10 = 100
    # 残留委托：远低于市价的买单（强平后会被自动撮合重新开仓）+ 保护多仓的 TPSL
    resting = buy(gw, 5, price=D("50"))  # 限价单不成交，保持挂单
    assert resting.id in gw._open
    gw.create_tpsl_order(
        TpslOrder(
            id="tpsl-1",
            contract=BTC,
            direction=1,
            kind="stop_loss",
            trigger_price=D("80"),
        )
    )
    gw.on_price(BTC, D("90"), D("89.9"), D("90.1"))  # 触发强平
    assert len(gw.liquidations) == 1
    assert gw._open == {}  # 残留挂单已清
    assert gw.list_tpsl_orders(BTC) == []  # 残留 TPSL 已清
    assert gw.list_orders(BTC, "open") == []  # 订单结果同步置 cancelled，无幽灵挂单
    gw.on_price(BTC, D("50"), D("49.9"), D("50.1"))  # 旧挂单价位：不得重新开仓
    assert gw.list_positions() == []
    assert [f for f in gw.drain_fills() if f.order_id == resting.id] == []


def test_average_entry_on_add():
    """验证同方向加仓后持仓张数与保证金累加、开仓均价按加权平均更新。

    参数：无

    返回：
        None，断言加仓后持仓 10 张、均价 110、保证金 1100
    """
    gw = make_gateway()
    buy(gw, 5)
    gw.on_price(BTC, D("120"), D("119.9"), D("120.1"))
    buy(gw, 5)
    pos = gw.list_positions()[0]
    assert pos.size == D("10") and pos.entry_price == D("110")
    assert pos.margin == D("1100")  # 500 + 600


def test_close_releases_margin():
    """验证平仓后保证金全额释放回可用余额，仅扣除开平两次手续费。

    参数：无

    返回：
        None，断言持仓清空、保证金归零、可用余额为 9999（仅扣 1 元双边费）
    """
    gw = make_gateway()
    buy(gw, 10)
    assert gw.account.positions[BTC].margin == D("1000")
    close_all(gw)
    assert gw.list_positions() == []
    assert gw.account.positions[BTC].margin == D("0")
    assert gw.account.available == D("9999")  # 保证金全额释放，仅扣两次手续费


def test_insufficient_balance_rejected():
    """验证委托所需保证金超过可用余额时下单被拒绝。

    参数：无

    返回：
        None，断言下 200 张单（需 20000 保证金）抛出 GatewayError
    """
    gw = make_gateway()
    with pytest.raises(GatewayError):
        buy(gw, 200)  # 需 20000 保证金 > 10000 可用


def test_set_leverage_reduces_margin():
    """验证设置杠杆后同等仓位按杠杆倍数降低保证金占用。

    参数：无

    返回：
        None，断言 5 倍杠杆下 10 张持仓保证金为 200、杠杆字段为 5
    """
    gw = make_gateway()
    gw.set_leverage(BTC, 5)
    buy(gw, 10)
    pos = gw.list_positions()[0]
    assert pos.leverage == D("5") and pos.margin == D("200")


def test_set_leverage_insufficient_balance_keeps_state():
    """验证调低杠杆需补保证金但余额不足时抛错，且杠杆与保证金保持原值（先校验后写入）。

    参数：无

    返回：
        None，断言抛 GatewayError 后持仓杠杆仍为 10、保证金不变
    """
    gw = make_gateway()
    gw.set_leverage(BTC, 10)
    buy(gw, 105)  # 保证金 1050，可用余额不足补足 1 倍杠杆的 10500
    before = gw.list_positions()[0]
    with pytest.raises(GatewayError):
        gw.set_leverage(BTC, 1)
    after = gw.list_positions()[0]
    assert after.leverage == D("10") and after.margin == before.margin


def test_set_leverage_cross_mode_surfaced():
    """验证 paper 持久化保证金模式：全仓按 Gate 口径映射 leverage=0 + cross_leverage_limit。

    参数：无

    返回：
        None，断言 cross/isolated 往返后持仓的 margin_mode、leverage、cross_leverage_limit 一致
    """
    gw = make_gateway()
    gw.set_leverage(BTC, 5, "cross")
    buy(gw, 10)
    pos = gw.list_positions()[0]
    assert pos.margin_mode == "cross"
    assert pos.leverage == D("0") and pos.cross_leverage_limit == D("5")
    gw.set_leverage(BTC, 3, "isolated")  # 切回逐仓后字段复原
    pos = gw.list_positions()[0]
    assert pos.margin_mode == "isolated"
    assert pos.leverage == D("3") and pos.cross_leverage_limit is None


def test_cancel_order():
    """验证撤销挂单成功后订单从挂单列表消失，重复撤同一单抛 OrderNotFound。

    参数：无

    返回：
        None，断言撤单结果 finish_as 为 cancelled、挂单清空、二次撤单抛 OrderNotFound
    """
    gw = make_gateway()
    result = buy(gw, 10, price=D("99"))
    cancelled = gw.cancel_order(BTC, result.id)
    assert cancelled.finish_as == "cancelled"
    assert gw.list_orders(BTC) == []
    with pytest.raises(OrderNotFound):
        gw.cancel_order(BTC, result.id)


def test_account_position_interface():
    """验证账户、持仓、权益与 ticker 只读接口的字段随行情更新保持一致。

    参数：无

    返回：
        None，断言可用余额、浮动盈亏、持仓四元组、总权益与 ticker 标记价均符合预期
    """
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
    """验证 K 线数据经注入的 provider 获取：未注入返回空列表，limit 与 from/to 同传被拒。

    参数：无

    返回：
        None，断言默认返回空、注入后透传哨兵对象、limit 与 from_ts 互斥抛 ValueError
    """
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
    """验证无反向持仓时 reduce_only 委托被拒绝。

    参数：无

    返回：
        None，断言无持仓下 reduce_only 卖单抛出 GatewayError
    """
    gw = make_gateway()
    with pytest.raises(GatewayError):
        gw.place_order(OrderRequest(contract=BTC, size=D(-5), reduce_only=True))


def test_close_without_position_is_noop():
    """验证无持仓时整仓平仓单为空操作：以 no_position 完成且不产生成交记录。

    参数：无

    返回：
        None，断言结果 finish_as 为 no_position、剩余量 0、成交列表为空
    """
    gw = make_gateway()
    result = close_all(gw)
    assert result.status == "finished" and result.finish_as == "no_position"
    assert result.left == D("0") and gw.account.fills == []  # 无持仓不记假成交


def test_open_interest_delegation():
    """验证持仓量查询的诚实降级与委托：未注入数据源返回 None，注入后透传其返回值。

    参数：无

    返回：
        None，断言默认网关返回 None、注入 oi_provider 后返回委托值 123456
    """
    # 未注入公共行情源（纯离线 mock 行情）：诚实降级 None
    assert make_gateway().fetch_open_interest(BTC) is None
    # 注入公共行情网关的 fetch_open_interest：委托真实数据（paper 非 mock 行情接线）
    cfg = PaperConfig(initial_equity=10000.0)
    gw = PaperGateway(cfg, contracts={BTC: make_contract()}, oi_provider=lambda c: D("123456"))
    assert gw.fetch_open_interest(BTC) == D("123456")


def test_open_interest_history_delegation():
    """验证历史持仓量查询透传参数给注入的 provider 并返回其数据，未注入时返回空列表。

    参数：无

    返回：
        None，断言 provider 收到 (合约, 粒度, 条数) 三元组、返回预设数据点，默认网关返回 []
    """
    points = [OpenInterestPoint(time=100, value=D("10"))]
    calls = []

    def provider(contract: str, interval: str, limit: int):
        """假的历史持仓量数据源：记录每次调用参数并返回预设数据点。

        参数：
            contract: str，合约名
            interval: str，时间粒度
            limit: int，返回条数上限

        返回：
            list：预设的 OpenInterestPoint 列表
        """
        calls.append((contract, interval, limit))
        return points

    cfg = PaperConfig(initial_equity=10000.0)
    gw = PaperGateway(cfg, oi_history_provider=provider)
    assert gw.fetch_open_interest_history(BTC, "4h", limit=3) == points
    assert calls == [(BTC, "4h", 3)]
    assert make_gateway().fetch_open_interest_history(BTC, "1d") == []
