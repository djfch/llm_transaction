"""止盈止损工具测试：开仓不变量、保护替换顺序与 paper 触发。"""

import time
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from src.agent.tool_handlers import ToolDeps
from src.agent.tools import ToolRegistry
from src.config import PaperConfig, RiskConfig
from src.gateway.base import (
    Account,
    Candle,
    Contract,
    GatewayError,
    OrderRequest,
    Position,
    TpslOrder,
)
from src.gateway.mock import MockGateway
from src.market.candles import CandleCache, ManualPriceSource
from src.market.triggers import TriggerManager
from src.memory import Database, Repo
from src.paper import PaperGateway
from src.risk.engine import RiskEngine
from src.risk.models import DailyStats


def _contract() -> Contract:
    """构造 BTC_USDT 永续合约的测试用合约元数据。

    参数：无

    返回：
        Contract：标记价 60000、最小下单 1 张的 BTC_USDT 合约定义
    """
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
    """提供当日统计的桩函数，固定返回零盈亏、零下单。

    参数：无

    返回：
        DailyStats：已实现盈亏为 0、当日下单数为 0 的统计对象
    """
    return DailyStats(realized_pnl=Decimal(0), orders_today=0)


async def _registry(tmp_path, gateway: MockGateway) -> SimpleNamespace:
    """组装一套带临时数据库与模拟网关的工具注册表测试环境。

    参数：
        tmp_path: pytest 临时目录夹具，SQLite 数据库文件落在其中
        gateway: MockGateway，模拟交易所网关，作为工具依赖注入

    返回：
        SimpleNamespace：含 db(数据库)、gateway(网关)、registry(工具注册表)、
        cache(K线缓存) 的测试环境命名空间
    """
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
        indicator_service=None,
        daily_stats_fn=_daily,
        round_id="r-tpsl",
    )
    return SimpleNamespace(db=db, gateway=gateway, registry=ToolRegistry(deps), cache=cache)


async def test_market_data_returns_beijing_ohlcv_table(tmp_path):
    """验证市场数据工具以北京时间表格返回 OHLCV 与完整性提示。

    参数：
        tmp_path: Path，pytest 提供的临时目录夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    gateway = MockGateway(contracts={"BTC_USDT": _contract()})
    recent = int(time.time()) - 1800  # 半小时前开盘的 1h K 线：新鲜数据（issue #74 停更判定）
    gateway.candles = [
        Candle(
            t=recent,
            o=Decimal("1"),
            h=Decimal("3"),
            l=Decimal("0.5"),
            c=Decimal("2"),
            v=Decimal("8"),
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
        assert "1970-01-01 08:00 | 1 | 2 | 3 | 0.5 | 8" not in out.text
        from datetime import timedelta, timezone

        beijing = datetime.fromtimestamp(recent, tz=timezone(timedelta(hours=8)))
        assert (
            f"{beijing:%Y-%m-%d %H:%M} | 1 | 2" in out.text
        )  # 北京时间行（尾部可能带未收盘标注，CI 时区无关）
    finally:
        await env.db.close()


async def test_market_data_marks_unclosed_candle(tmp_path):
    """未收盘标注：窗口未结束的最后一根尾部追加（未收盘），已收盘根不标。

    参数：
        tmp_path: Path，pytest 提供的临时目录夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
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
    """验证开仓必须提供方向正确的止损并把请求杠杆应用到持仓。

    参数：
        tmp_path: Path，pytest 提供的临时目录夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    gateway = MockGateway(contracts={"BTC_USDT": _contract()})
    env = await _registry(tmp_path, gateway)
    try:
        missing = await env.registry.execute(
            "place_order",
            {"contract": "BTC_USDT", "side": "long", "margin_usdt": 20, "leverage": 3},
        )
        wrong = await env.registry.execute(
            "place_order",
            {
                "contract": "BTC_USDT",
                "side": "long",
                "margin_usdt": 20,
                "leverage": 3,
                "stop_loss_price": 61000,
            },
        )
        ok = await env.registry.execute(
            "place_order",
            {
                "contract": "BTC_USDT",
                "side": "long",
                "margin_usdt": 20,
                "leverage": 3,
                "stop_loss_price": 58000,
                "take_profit_price": 64000,
            },
        )
        assert "缺少必填参数 stop_loss_price" in missing.text and "低于标记价" in wrong.text
        assert ok.risk_verdict == "allow"
        assert gateway.placed[-1].stop_loss_price == Decimal(58000)
        tpsl = {o.kind: o.trigger_price for o in gateway.list_tpsl_orders("BTC_USDT")}
        assert tpsl["stop_loss"] == Decimal(58000)
        assert tpsl["take_profit"] == Decimal(64000)
        assert gateway.positions["BTC_USDT"].leverage == Decimal(3)
    finally:
        await env.db.close()


def _long_position(*, entry: str = "60000") -> Position:
    """构造 10 张 BTC 多仓供整仓止损金额测试。

    参数：
        entry: str，整仓开仓均价

    返回：
        Position：标记价 60000、逐仓 2 倍的多仓
    """
    return Position(
        contract="BTC_USDT",
        size=Decimal(10),
        entry_price=Decimal(entry),
        mark_price=Decimal(60000),
        liq_price=Decimal(0),
        leverage=Decimal(2),
        margin=Decimal(0),
        unrealised_pnl=Decimal(0),
    )


def _install_stop(gateway: MockGateway, price: str) -> None:
    """给测试多仓安装一张当前整仓止损单。

    参数：
        gateway: MockGateway，待写入保护单的网关
        price: str，止损触发价

    返回：
        None，就地写入一张 stop_loss 保护单
    """
    gateway.create_tpsl_order(
        TpslOrder(
            id="",
            contract="BTC_USDT",
            direction=1,
            kind="stop_loss",
            trigger_price=Decimal(price),
        )
    )


async def test_update_tpsl_rejects_over_limit_widening_but_allows_tightening(tmp_path):
    """校验当前已超限时放宽止损被拒，确实收紧风险则允许。

    参数：
        tmp_path: Path，pytest 临时目录

    返回：
        None，断言 20 USDT 当前风险可收紧到 15，但不能放宽到 30
    """
    gateway = MockGateway(
        contracts={"BTC_USDT": _contract()},
        account=Account(available=Decimal(1000), unrealised_pnl=Decimal(0)),
        positions={"BTC_USDT": _long_position()},
    )
    _install_stop(gateway, "58000")
    env = await _registry(tmp_path, gateway)
    try:
        widened = await env.registry.execute(
            "update_tpsl", {"contract": "BTC_USDT", "stop_loss_price": 57000}
        )
        tightened = await env.registry.execute(
            "update_tpsl", {"contract": "BTC_USDT", "stop_loss_price": 58500}
        )
        assert widened.risk_verdict == "deny" and "没有缩小当前风险" in widened.text
        assert tightened.risk_verdict == "allow"
        assert "计划止损估算 15" in tightened.text
    finally:
        await env.db.close()


async def test_update_tpsl_first_protection_over_limit_is_allowed_with_warning(tmp_path):
    """校验无止损仓位首次补保护即使仍超 1% 也放行并明确警告。

    参数：
        tmp_path: Path，pytest 临时目录

    返回：
        None，断言新止损生效且结果要求继续收紧
    """
    gateway = MockGateway(
        contracts={"BTC_USDT": _contract()},
        account=Account(available=Decimal(1000), unrealised_pnl=Decimal(0)),
        positions={"BTC_USDT": _long_position()},
    )
    env = await _registry(tmp_path, gateway)
    try:
        outcome = await env.registry.execute(
            "update_tpsl", {"contract": "BTC_USDT", "stop_loss_price": 57000}
        )
        assert outcome.risk_verdict == "allow"
        assert "首次保护止损仍超过风险上限" in outcome.text
    finally:
        await env.db.close()


async def test_update_tpsl_profit_zone_has_zero_planned_loss(tmp_path):
    """校验止损移入盈利区时计划亏损为零而不是取绝对值。

    参数：
        tmp_path: Path，pytest 临时目录

    返回：
        None，断言开仓均价 58000、止损 59000 的计划止损估算为零
    """
    gateway = MockGateway(
        contracts={"BTC_USDT": _contract()},
        account=Account(available=Decimal(1000), unrealised_pnl=Decimal(0)),
        positions={"BTC_USDT": _long_position(entry="58000")},
    )
    _install_stop(gateway, "57000")
    env = await _registry(tmp_path, gateway)
    try:
        outcome = await env.registry.execute(
            "update_tpsl", {"contract": "BTC_USDT", "stop_loss_price": 59000}
        )
        assert outcome.risk_verdict == "allow"
        assert "计划止损估算 0" in outcome.text
    finally:
        await env.db.close()


class _UnavailableContractGateway(MockGateway):
    """持仓与保护单可读，但实时合约规格接口不可用。"""

    def get_contract(self, contract: str) -> Contract:
        """模拟官方规格查询故障。

        参数：
            contract: str，合约名

        返回：
            Contract：本实现不会返回

        异常：
            GatewayError：始终抛出规格查询故障
        """
        raise GatewayError(f"{contract} 规格查询故障")


async def test_update_tpsl_allows_tightening_when_contract_query_fails(tmp_path):
    """校验规格接口故障不能阻断已有止损的严格收紧。

    参数：
        tmp_path: Path，pytest 临时目录

    返回：
        None，断言 58000 收紧到 58500 后新保护单生效
    """
    gateway = _UnavailableContractGateway(
        contracts={"BTC_USDT": _contract()},
        account=Account(available=Decimal(1000), unrealised_pnl=Decimal(0)),
        positions={"BTC_USDT": _long_position()},
    )
    _install_stop(gateway, "58000")
    env = await _registry(tmp_path, gateway)
    try:
        outcome = await env.registry.execute(
            "update_tpsl", {"contract": "BTC_USDT", "stop_loss_price": 58500}
        )
        assert outcome.risk_verdict == "allow", outcome.text
        assert gateway.list_tpsl_orders("BTC_USDT")[0].trigger_price == Decimal(58500)
    finally:
        await env.db.close()


async def test_update_tpsl_allows_first_protection_when_contract_query_fails(tmp_path):
    """校验无旧止损时，规格故障也不能阻断首次建立方向正确的保护。

    参数：
        tmp_path: Path，pytest 临时目录

    返回：
        None，断言首次止损成功建立并提示估算暂不可用
    """
    gateway = _UnavailableContractGateway(
        contracts={"BTC_USDT": _contract()},
        account=Account(available=Decimal(1000), unrealised_pnl=Decimal(0)),
        positions={"BTC_USDT": _long_position()},
    )
    env = await _registry(tmp_path, gateway)
    try:
        outcome = await env.registry.execute(
            "update_tpsl", {"contract": "BTC_USDT", "stop_loss_price": 58000}
        )
        assert outcome.risk_verdict == "allow", outcome.text
        assert "规格不可用" in outcome.text
    finally:
        await env.db.close()


async def test_update_tpsl_allows_tightening_during_delisting(tmp_path):
    """校验合约进入下架期后仍可收紧已有持仓止损。

    参数：
        tmp_path: Path，pytest 临时目录

    返回：
        None，断言下架状态不阻断 58000 到 58500 的降险更新
    """
    contract = _contract().model_copy(update={"status": "delisting", "in_delisting": True})
    gateway = MockGateway(
        contracts={"BTC_USDT": contract},
        account=Account(available=Decimal(1000), unrealised_pnl=Decimal(0)),
        positions={"BTC_USDT": _long_position()},
    )
    _install_stop(gateway, "58000")
    env = await _registry(tmp_path, gateway)
    try:
        outcome = await env.registry.execute(
            "update_tpsl", {"contract": "BTC_USDT", "stop_loss_price": 58500}
        )
        assert outcome.risk_verdict == "allow", outcome.text
    finally:
        await env.db.close()


@pytest.mark.parametrize(
    ("size", "loose_stop", "tight_stop", "requested_stop"),
    [
        (Decimal(10), Decimal(57000), Decimal(59000), Decimal(58000)),
        (Decimal(-10), Decimal(63000), Decimal(61000), Decimal(62000)),
    ],
)
async def test_update_tpsl_uses_tightest_existing_stop_independent_of_order(
    tmp_path, size: Decimal, loose_stop: Decimal, tight_stop: Decimal, requested_stop: Decimal
):
    """校验多张旧止损按实际最强保护比较，不依赖接口返回顺序。

    参数：
        tmp_path: Path，pytest 临时目录
        size: Decimal，正多负空的整仓张数
        loose_stop: Decimal，先返回的宽松旧止损
        tight_stop: Decimal，后返回的严格旧止损
        requested_stop: Decimal，介于两者之间的新止损

    返回：
        None，断言相对最紧止损属于放宽且在超限时被拒绝
    """
    position = _long_position().model_copy(update={"size": size})
    direction = 1 if size > 0 else -1
    gateway = MockGateway(
        contracts={"BTC_USDT": _contract()},
        account=Account(available=Decimal(1000), unrealised_pnl=Decimal(0)),
        positions={"BTC_USDT": position},
    )
    for stop in (loose_stop, tight_stop):
        gateway.create_tpsl_order(
            TpslOrder(
                id="",
                contract="BTC_USDT",
                direction=direction,
                kind="stop_loss",
                trigger_price=stop,
            )
        )
    env = await _registry(tmp_path, gateway)
    try:
        outcome = await env.registry.execute(
            "update_tpsl",
            {"contract": "BTC_USDT", "stop_loss_price": requested_stop},
        )
        assert outcome.risk_verdict == "deny"
        assert "没有缩小当前风险" in outcome.text
    finally:
        await env.db.close()


class _TraceGateway(MockGateway):
    def __init__(self) -> None:
        """初始化带 BTC 合约、充足余额和止盈止损事件轨迹的网关。

        参数：
            无

        返回：
            None：初始化基础模拟网关并创建空事件列表
        """
        super().__init__(
            contracts={"BTC_USDT": _contract()},
            account=Account(available=Decimal("10000"), unrealised_pnl=Decimal(0)),
        )
        self.events: list[str] = []

    def create_tpsl_order(self, order: TpslOrder) -> TpslOrder:
        """记录保护单创建顺序后交由基础网关创建订单。

        参数：
            order: TpslOrder，待创建的止盈或止损订单

        返回：
            TpslOrder：基础模拟网关创建并保存的保护单
        """
        self.events.append(f"create:{order.kind}")
        return super().create_tpsl_order(order)

    def cancel_tpsl_order(self, order_id: str) -> None:
        """记录保护单撤销顺序后交由基础网关撤销订单。

        参数：
            order_id: str，交易所订单编号

        返回：
            None：记录订单编号并撤销对应保护单
        """
        self.events.append(f"cancel:{order_id}")
        super().cancel_tpsl_order(order_id)


async def test_update_tpsl_creates_full_new_group_before_cancelling_old(tmp_path):
    """验证更新止盈止损时先完整创建新组再撤销旧组。

    参数：
        tmp_path: Path，pytest 提供的临时目录夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
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
        """模拟创建新止盈单失败，止损单仍交由基础网关创建。

        参数：
            order: TpslOrder，待创建的止盈或止损订单

        返回：
            TpslOrder：非止盈单由基础网关创建后返回

        异常：
            GatewayError：订单类型为 take_profit 时模拟创建失败
        """
        if order.kind == "take_profit":
            raise GatewayError("创建止盈失败", label="CREATE_FAILED")
        return super().create_tpsl_order(order)


async def test_update_tpsl_rolls_back_new_group_when_creation_fails(tmp_path):
    """验证新止盈单创建失败时回滚已创建的新止损并保留旧保护单。

    参数：
        tmp_path: Path，pytest 提供的临时目录夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
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
        """模拟撤销旧止盈单失败，其他订单仍交由基础网关撤销。

        参数：
            order_id: str，交易所订单编号

        返回：
            None：非目标止盈单被基础网关撤销

        异常：
            GatewayError：订单编号等于指定旧止盈单编号时模拟撤销失败
        """
        if order_id == self.fail_id:
            raise GatewayError("撤销旧止盈失败", label="CANCEL_FAILED")
        super().cancel_tpsl_order(order_id)


async def test_update_tpsl_reports_partial_old_cancel_failure(tmp_path):
    """验证旧保护单部分撤销失败时保留新组并明确报告残留风险。

    参数：
        tmp_path: Path，pytest 提供的临时目录夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
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
    """验证模拟止损触发后平仓并把成交来源标记为 tpsl_close。

    参数：
        无

    返回：
        None：通过断言校验目标场景，无返回值
    """
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
    """验证模拟限价开仓成交后请求中的止损仍绑定到新持仓。

    参数：
        无

    返回：
        None：通过断言校验目标场景，无返回值
    """
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
    """验证修改限价单后成交仍保留原始止损配置。

    参数：
        无

    返回：
        None：通过断言校验目标场景，无返回值
    """
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
