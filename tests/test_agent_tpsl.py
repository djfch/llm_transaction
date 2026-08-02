"""止盈止损工具测试：开仓不变量、保护替换顺序与 paper 触发。"""

import time
from decimal import Decimal
from types import SimpleNamespace

from src.agent.tool_handlers import ToolDeps
from src.agent.tools import ToolRegistry
from src.config import PaperConfig, RiskConfig
from src.gateway.base import Account, Candle, Contract, GatewayError, OrderRequest, TpslOrder
from src.gateway.mock import MockGateway
from src.market.candles import CandleCache, ManualPriceSource
from src.market.triggers import TriggerManager
from src.memory import Database, Repo
from src.paper import PaperGateway
from src.risk.engine import RiskEngine
from src.risk.models import DailyStats


def _contract() -> Contract:
    return Contract(
        name="BTC_USDT",
        quanto_multiplier=Decimal("0.001"),
        order_size_min=Decimal(1),
        order_size_max=Decimal("1000000"),
        order_price_round=Decimal("0.1"),
        enable_decimal=False,
        mark_price=Decimal("60000"),
        funding_rate=Decimal(0),
        funding_interval=0,
        maker_fee_rate=Decimal("0.0002"),
        taker_fee_rate=Decimal("0.0005"),
        status="trading",
        in_delisting=False,
    )


async def _daily() -> DailyStats:
    return DailyStats(realized_pnl=Decimal(0), orders_today=0)


async def _registry(tmp_path, gateway: MockGateway) -> SimpleNamespace:
    db = Database()
    await db.open(tmp_path / "tpsl.db")
    repo = Repo(db)
    cache = CandleCache(gateway, ManualPriceSource())
    deps = ToolDeps(
        gateway=gateway,
        risk_engine=RiskEngine(),
        risk_config=RiskConfig(),
        watchlist=["BTC_USDT"],
        repo=repo,
        candles=cache,
        triggers=TriggerManager(lambda _t, _p: None),
        daily_stats_fn=_daily,
        round_id="r-tpsl",
    )
    return SimpleNamespace(db=db, gateway=gateway, registry=ToolRegistry(deps), cache=cache)


async def test_market_data_returns_beijing_ohlcv_table(tmp_path):
    gateway = MockGateway(contracts={"BTC_USDT": _contract()})
    gateway.candles = [
        Candle(
            t=0, o=Decimal("1"), h=Decimal("3"), l=Decimal("0.5"), c=Decimal("2"), v=Decimal("8")
        )
    ]
    env = await _registry(tmp_path, gateway)
    try:
        env.cache.backfill(["BTC_USDT"], ["1h"], limit=1)
        out = await env.registry.execute(
            "get_market_data", {"contract": "BTC_USDT", "interval": "1h"}
        )
        assert "交易对：BTC_USDT；时间尺度：1h；时间：北京时间（UTC+8）" in out.text
        assert "时间（年月日时分） | 开盘价 | 收盘价 | 最高价格 | 最低价格 | 交易量" in out.text
        assert "1970-01-01 08:00 | 1 | 2 | 3 | 0.5 | 8" in out.text
    finally:
        await env.db.close()


async def test_market_data_marks_unclosed_candle(tmp_path):
    """未收盘标注：窗口未结束的最后一根尾部追加（未收盘），已收盘根不标。"""
    now = int(time.time())
    current_open = now - (now % 3600)  # 当前 1h 窗口的开盘时刻
    prev_open = current_open - 3600
    gateway = MockGateway(contracts={"BTC_USDT": _contract()})
    gateway.candles = [
        Candle(
            t=prev_open,
            o=Decimal("1"),
            h=Decimal("3"),
            l=Decimal("0.5"),
            c=Decimal("2"),
            v=Decimal("8"),
        ),
        Candle(
            t=current_open,
            o=Decimal("2"),
            h=Decimal("4"),
            l=Decimal("1"),
            c=Decimal("3"),
            v=Decimal("5"),
        ),
    ]
    env = await _registry(tmp_path, gateway)
    try:
        env.cache.backfill(["BTC_USDT"], ["1h"], limit=2)
        out = await env.registry.execute(
            "get_market_data", {"contract": "BTC_USDT", "interval": "1h"}
        )
        rows = [ln for ln in out.text.splitlines() if ln[:1].isdigit()]
        assert len(rows) == 2
        assert "（未收盘）" not in rows[0]
        assert rows[1].endswith(" （未收盘）")
        assert out.text.count("（未收盘）") == 1
    finally:
        await env.db.close()


async def test_open_requires_valid_stop_and_applies_leverage(tmp_path):
    gateway = MockGateway(contracts={"BTC_USDT": _contract()})
    env = await _registry(tmp_path, gateway)
    try:
        missing = await env.registry.execute("place_order", {"contract": "BTC_USDT", "size": 1})
        wrong = await env.registry.execute(
            "place_order", {"contract": "BTC_USDT", "size": 1, "stop_loss_price": 61000}
        )
        ok = await env.registry.execute(
            "place_order",
            {
                "contract": "BTC_USDT",
                "size": 1,
                "leverage": 3,
                "stop_loss_price": 58000,
                "take_profit_price": 64000,
            },
        )
        assert "必须提供" in missing.text and "低于标记价" in wrong.text
        assert ok.risk_verdict == "allow"
        assert gateway.placed[-1].stop_loss_price == Decimal(58000)
        assert gateway.list_positions()[0].stop_loss_price == Decimal(58000)
        assert gateway.list_positions()[0].take_profit_price == Decimal(64000)
        assert gateway.positions["BTC_USDT"].leverage == Decimal(3)
    finally:
        await env.db.close()


class _TraceGateway(MockGateway):
    def __init__(self) -> None:
        super().__init__(
            contracts={"BTC_USDT": _contract()},
            account=Account(available=Decimal("10000"), unrealised_pnl=Decimal(0)),
        )
        self.events: list[str] = []

    def create_tpsl_order(self, order: TpslOrder) -> TpslOrder:
        self.events.append(f"create:{order.kind}")
        return super().create_tpsl_order(order)

    def cancel_tpsl_order(self, order_id: str) -> None:
        self.events.append(f"cancel:{order_id}")
        super().cancel_tpsl_order(order_id)


async def test_update_tpsl_creates_full_new_group_before_cancelling_old(tmp_path):
    gateway = _TraceGateway()
    gateway.place_order(OrderRequest(contract="BTC_USDT", size=Decimal(1)))
    old_stop = gateway.create_tpsl_order(
        TpslOrder(
            id="", contract="BTC_USDT", direction=1, kind="stop_loss", trigger_price=Decimal(57000)
        )
    )
    old_take = gateway.create_tpsl_order(
        TpslOrder(
            id="",
            contract="BTC_USDT",
            direction=1,
            kind="take_profit",
            trigger_price=Decimal(65000),
        )
    )
    gateway.events.clear()
    env = await _registry(tmp_path, gateway)
    try:
        out = await env.registry.execute(
            "update_tpsl",
            {"contract": "BTC_USDT", "stop_loss_price": 58000, "take_profit_price": 64000},
        )
        assert out.risk_verdict == "allow", out.text
        assert gateway.events[:2] == ["create:stop_loss", "create:take_profit"]
        assert gateway.events[2:] == [f"cancel:{old_stop.id}", f"cancel:{old_take.id}"]
        assert {o.trigger_price for o in gateway.list_tpsl_orders("BTC_USDT")} == {
            Decimal(58000),
            Decimal(64000),
        }
    finally:
        await env.db.close()


class _CreateTakeProfitFailsGateway(_TraceGateway):
    def create_tpsl_order(self, order: TpslOrder) -> TpslOrder:
        if order.kind == "take_profit":
            raise GatewayError("创建止盈失败", label="CREATE_FAILED")
        return super().create_tpsl_order(order)


async def test_update_tpsl_rolls_back_new_group_when_creation_fails(tmp_path):
    gateway = _CreateTakeProfitFailsGateway()
    gateway.place_order(OrderRequest(contract="BTC_USDT", size=Decimal(1)))
    old = gateway.create_tpsl_order(
        TpslOrder(
            id="", contract="BTC_USDT", direction=1, kind="stop_loss", trigger_price=Decimal(57000)
        )
    )
    env = await _registry(tmp_path, gateway)
    try:
        out = await env.registry.execute(
            "update_tpsl",
            {"contract": "BTC_USDT", "stop_loss_price": 58000, "take_profit_price": 64000},
        )
        assert "新保护单已回滚" in out.text
        assert gateway.list_tpsl_orders("BTC_USDT") == [old]
    finally:
        await env.db.close()


class _CancelOldTakeProfitFailsGateway(_TraceGateway):
    fail_id: str = ""

    def cancel_tpsl_order(self, order_id: str) -> None:
        if order_id == self.fail_id:
            raise GatewayError("撤销旧止盈失败", label="CANCEL_FAILED")
        super().cancel_tpsl_order(order_id)


async def test_update_tpsl_reports_partial_old_cancel_failure(tmp_path):
    gateway = _CancelOldTakeProfitFailsGateway()
    gateway.place_order(OrderRequest(contract="BTC_USDT", size=Decimal(1)))
    old_stop = gateway.create_tpsl_order(
        TpslOrder(
            id="", contract="BTC_USDT", direction=1, kind="stop_loss", trigger_price=Decimal(57000)
        )
    )
    old_take = gateway.create_tpsl_order(
        TpslOrder(
            id="",
            contract="BTC_USDT",
            direction=1,
            kind="take_profit",
            trigger_price=Decimal(65000),
        )
    )
    gateway.fail_id = old_take.id
    env = await _registry(tmp_path, gateway)
    try:
        out = await env.registry.execute(
            "update_tpsl",
            {"contract": "BTC_USDT", "stop_loss_price": 58000, "take_profit_price": 64000},
        )
        assert "仅撤销 1/2 个" in out.text
        remaining = gateway.list_tpsl_orders("BTC_USDT")
        assert {item.id for item in remaining} == {old_take.id, "tpsl-4", "tpsl-5"}
        assert old_stop.id not in {item.id for item in remaining}
    finally:
        await env.db.close()


def test_paper_stop_loss_closes_and_marks_tpsl_source():
    gateway = PaperGateway(PaperConfig(initial_equity=10000), contracts={"BTC_USDT": _contract()})
    gateway.on_price("BTC_USDT", Decimal(60000))
    gateway.place_order(
        OrderRequest(contract="BTC_USDT", size=Decimal(1), stop_loss_price=Decimal(58000))
    )
    gateway.on_price("BTC_USDT", Decimal(57900))
    fills = gateway.drain_fills()
    assert gateway.list_positions() == []
    assert fills[-1].order_id.startswith("tpsl-") and fills[-1].is_close


def test_paper_limit_fill_keeps_stop_loss():
    gateway = PaperGateway(PaperConfig(initial_equity=10000), contracts={"BTC_USDT": _contract()})
    gateway.on_price("BTC_USDT", Decimal(60000), Decimal(59999), Decimal(60001))
    result = gateway.place_order(
        OrderRequest(
            contract="BTC_USDT",
            size=Decimal(1),
            price=Decimal(59000),
            stop_loss_price=Decimal(58000),
        )
    )
    assert result.status == "open"
    gateway.on_price("BTC_USDT", Decimal(59000), Decimal(58999), Decimal(59000))
    assert gateway.list_positions()[0].stop_loss_price == Decimal(58000)


def test_paper_amended_limit_fill_keeps_stop_loss():
    gateway = PaperGateway(PaperConfig(initial_equity=10000), contracts={"BTC_USDT": _contract()})
    gateway.on_price("BTC_USDT", Decimal(60000), Decimal(59999), Decimal(60001))
    result = gateway.place_order(
        OrderRequest(
            contract="BTC_USDT",
            size=Decimal(1),
            price=Decimal(59000),
            stop_loss_price=Decimal(58000),
        )
    )
    assert result.status == "open"
    gateway.amend_order("BTC_USDT", result.id, price=Decimal(61000))
    assert gateway.list_positions()[0].stop_loss_price == Decimal(58000)
