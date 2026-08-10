"""模拟撮合测试：翻仓原子性、挂单余额不足、订单 ID 唯一性、已实现盈亏口径、
无持仓 close 不产生假成交，以及 drain_fills 接口。"""

import re
from decimal import Decimal

import pytest

from src.config import PaperConfig
from src.gateway.base import Contract, GatewayError, OrderRequest
from src.paper import PaperAccount, PaperGateway

BTC = "BTC_USDT"
D = Decimal


def make_contract(taker: str = "0.0005", maker: str = "0.0002") -> Contract:
    """构造 BTC_USDT 模拟合约，手续费率可调，其余交易参数固定。

    参数：
        taker: str，吃单费率文本
        maker: str，挂单费率文本
    返回：
        Contract，返回该测试辅助函数构造或记录的结果
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
    """构造已喂入初始行情的模拟网关，初始权益 10000、维护保证金率 0.005。

    参数：
        slippage: str，滑点比例文本
        taker: str，吃单费率文本
    返回：
        PaperGateway，返回该测试辅助函数构造或记录的结果
    """
    cfg = PaperConfig(initial_equity=10000.0, slippage=float(slippage))
    gw = PaperGateway(cfg, contracts={BTC: make_contract(taker=taker)}, maintenance_rate=D("0.005"))
    gw.on_price(BTC, D("100"), D("99.9"), D("100.1"))
    return gw


def buy(gw: PaperGateway, size, price=None):
    """提交 BTC_USDT 下单请求的便捷辅助，省去重复构造 OrderRequest。

    参数：
        gw: PaperGateway，模拟交易网关
        size: int，订单张数
        price: Decimal，最新成交价
    返回：
        Order，模拟网关生成的订单
    """
    return gw.place_order(OrderRequest(contract=BTC, size=D(size), price=price))


def close_all(gw: PaperGateway):
    """提交 BTC_USDT 一键全平请求的便捷辅助（close=True，无持仓时也安全返回）。

    参数：
        gw: PaperGateway，模拟交易网关
    返回：
        Order，模拟网关生成的全平订单
    """
    return gw.place_order(OrderRequest(contract=BTC, close=True))


def test_flip_insufficient_balance_is_atomic():
    """翻仓开仓余额不足时整单拒绝，持仓/余额/成交记录与下单前完全一致。

    参数：无
    返回：
        None，执行断言验证目标行为
    """
    gw = make_gateway()
    buy(gw, 10)  # 多 10 张：保证金 1000，可用 8999.5，手续费 0.5
    fills_before = len(gw.account.fills)
    with pytest.raises(GatewayError):
        buy(gw, -250)  # 平 10 后需再开 240 空：24000 保证金，平仓返还后仍不够
    pos = gw.account.position(BTC)
    assert pos.size == D("10") and pos.margin == D("1000")
    assert gw.account.available == D("8999.5")
    assert len(gw.account.fills) == fills_before
    assert gw.account.total_realized == D("0")
    assert gw.account.total_fee == D("0.5")


def test_flip_still_works_when_affordable():
    """余额足够时翻仓正常执行（先平后开）。

    参数：无
    返回：
        None，执行断言验证目标行为
    """
    gw = make_gateway()
    buy(gw, 10)
    result = buy(gw, -20)  # 平 10 多、开 10 空：需 1000 保证金 + 1 费，足够
    assert result.finish_as == "filled"
    pos = gw.account.position(BTC)
    assert pos.size == D("-10") and pos.entry_price == D("100")


def test_resting_order_insufficient_balance_cancelled_not_raised():
    """挂单触价但余额不足时撤单且不抛异常，后续 tick 与强平检查继续执行。

    参数：无
    返回：
        None，执行断言验证目标行为
    """
    gw = make_gateway(taker="0")
    gw.set_leverage(BTC, 10)
    buy(gw, 10)  # 保证金 100，可用 9900
    result = buy(gw, 5000, price=D("95"))  # ask 100.1 > 95 → 挂单
    assert result.status == "open"
    # 触价：ask 94.1 ≤ 95，需保证金 5000×94.1/10=47050 > 9900 → 撤单而非抛异常
    gw.on_price(BTC, D("94"), D("93.9"), D("94.1"))
    assert gw.list_orders(BTC) == []
    finished = gw.list_orders(BTC, status="finished")
    assert finished[-1].id == result.id
    assert finished[-1].finish_as.startswith("cancelled")
    assert gw.account.position(BTC).size == D("10")  # 原持仓不受影响
    # 后续 tick：被撤挂单不再重复触发，且强平检查照常执行
    gw.on_price(BTC, D("89"), D("88.9"), D("89.1"))  # 保证金率 < 0 → 强平
    assert len(gw.liquidations) == 1
    assert gw.list_positions() == []


def test_order_ids_globally_unique_across_instances():
    """订单 ID 全局唯一（t- + 26 位 hex），两个实例先后下单不撞主键。

    参数：无
    返回：
        None，执行断言验证目标行为
    """
    gw1 = make_gateway()
    gw2 = make_gateway()
    r1, r2 = buy(gw1, 1), buy(gw2, 1)
    assert r1.id != r2.id
    assert re.fullmatch(r"t-[0-9a-f]{26}", r1.id)
    assert re.fullmatch(r"t-[0-9a-f]{26}", r2.id)


def test_total_realized_capped_on_bankrupt_liquidation():
    """穿仓强平时 total_realized 以保证金为限，与余额实际变动口径一致。

    参数：无
    返回：
        None，执行断言验证目标行为
    """
    gw = make_gateway(taker="0")
    gw.set_leverage(BTC, 10)
    buy(gw, 10)  # 保证金 100，可用 9900
    gw.on_price(BTC, D("80"), D("79.9"), D("80.1"))  # 浮亏 -200 > 保证金 100 → 穿仓强平
    assert len(gw.liquidations) == 1
    assert gw.account.available == D("9900")  # 保证金全亏，无返还
    assert gw.account.total_realized == D("-100")  # 不记原始 -200


def test_reduce_realized_capped_at_released_margin():
    """账本层平仓亏损以释放保证金为限，FillRecord 与余额同口径。

    参数：无
    返回：
        None，执行断言验证目标行为
    """
    acc = PaperAccount(D("10000"))
    acc.apply_fill("o1", BTC, D("10"), D("100"), D("1"), D("10"), D("0"), False)
    rec = acc.apply_fill("o2", BTC, D("-10"), D("80"), D("1"), D("10"), D("0"), False)
    assert acc.available == D("9900")  # 返还 max(100-200, 0) = 0
    assert rec.realized_pnl == D("-100")
    assert acc.total_realized == D("-100")


def test_close_without_position_returns_no_position():
    """无持仓 close 不伪装成交：无 FillRecord、fill_price=0、标记 no_position。

    参数：无
    返回：
        None，执行断言验证目标行为
    """
    gw = make_gateway()
    result = close_all(gw)
    assert result.status == "finished"
    assert result.finish_as == "no_position"
    assert result.left == D("0") and result.fill_price == D("0")
    assert gw.account.fills == []


def test_close_without_position_needs_no_market_data():
    """无行情且无持仓时 close 正常返回，不抛 NO_MARKET_DATA。

    参数：无
    返回：
        None，执行断言验证目标行为
    """
    cfg = PaperConfig(initial_equity=10000.0)
    gw = PaperGateway(cfg, contracts={BTC: make_contract()})
    result = close_all(gw)  # 从未 on_price
    assert result.status == "finished" and result.finish_as == "no_position"


def test_drain_fills_returns_and_clears_buffer():
    """drain_fills：返回自上次调用以来的全部成交并清空缓冲。

    参数：无
    返回：
        None，执行断言验证目标行为
    """
    gw = make_gateway()
    buy(gw, 10)
    buy(gw, -5)
    fills = gw.drain_fills()
    assert len(fills) == 2
    assert fills[0].size == D("10") and fills[0].realized_pnl == D("0")
    assert fills[1].size == D("-5") and fills[1].realized_pnl == D("0")
    assert fills[0].order_id and fills[0].contract == BTC
    assert gw.drain_fills() == []


def test_reset_account_clears_positions_orders_fills():
    """账户重置：有持仓+挂单+成交缓冲时重置 → 权益为新值，仓位/挂单/fills/强平记录全清。

    参数：无
    返回：
        None，执行断言验证目标行为
    """
    gw = make_gateway()
    buy(gw, 10)  # 持仓 + 成交缓冲
    result = buy(gw, 5, price=D("99"))  # 未成交挂单
    assert result.status == "open"
    assert gw.list_positions() and gw.list_orders(BTC) and gw.account.fills
    gw.reset_account(D("5000"))
    assert gw.equity() == D("5000")  # 新权益（含保证金/浮盈口径，空仓即余额）
    assert gw.account.available == D("5000")
    assert gw.list_positions() == []
    assert gw.list_orders(BTC) == []  # 未成交挂单一并清空
    assert gw.drain_fills() == []
    assert gw.liquidations == []  # 与账户联动的强平记录一并重置
    buy(gw, 1)  # 重置后仍可正常交易
    assert gw.list_positions()[0].size == D("1")


def test_fill_is_close_flags():
    """FillRecord.is_close：开仓 False；减仓/平仓/翻仓（含平仓部分）True。

    参数：无
    返回：
        None，执行断言验证目标行为
    """
    gw = make_gateway()
    buy(gw, 10)
    buy(gw, 5)  # 加仓仍属开仓
    buy(gw, -3)  # 减仓
    buy(gw, -20)  # 平 12 并翻空 8：含平仓部分记 True
    fills = gw.drain_fills()
    assert [f.is_close for f in fills] == [False, False, True, True]
    close_all(gw)  # 全平
    assert gw.drain_fills()[0].is_close is True


def test_liquidation_fill_is_close():
    """强平成交的 FillRecord.is_close=True（供落库 trades.source=liquidation 判定）。

    参数：无
    返回：
        None，执行断言验证目标行为
    """
    gw = make_gateway(taker="0")
    gw.set_leverage(BTC, 10)
    buy(gw, 10)
    gw.on_price(BTC, D("90"), D("89.9"), D("90.1"))  # 触发强平
    fills = [f for f in gw.drain_fills() if f.order_id == "liquidation"]
    assert len(fills) == 1
    assert fills[0].is_close is True
