"""现有 place_order 的保证金接口、整仓风险和降险语义测试。"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from src.agent.tool_handlers import ToolDeps
from src.agent.tools import ToolRegistry
from src.config import RiskConfig
from src.gateway.base import Account, Contract, GatewayError, Position
from src.gateway.mock import MockGateway
from src.market.candles import CandleCache, ManualPriceSource
from src.market.triggers import TriggerManager
from src.memory import Database, Repo
from src.risk.engine import RiskEngine
from src.risk.models import DailyStats

D = Decimal


def _contract(*, decimal_size: bool = False, minimum: str = "1", maximum: str = "1000") -> Contract:
    """构造标记价 100、每张 1 币的测试合约。

    参数：
        decimal_size: bool，是否支持小数张
        minimum: str，最小张数
        maximum: str，最大张数

    返回：
        Contract：可交易的 BTC_USDT 测试规格
    """
    return Contract(
        name="BTC_USDT",
        quanto_multiplier=D(1),
        order_size_min=D(minimum),
        order_size_max=D(maximum),
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


def _position(
    *, size: str, entry: str = "100", margin: str = "0", mode: str = "isolated"
) -> Position:
    """构造指定方向、均价和保证金模式的持仓。

    参数：
        size: str，带方向张数
        entry: str，整仓开仓均价
        margin: str，当前占用保证金
        mode: str，保证金模式

    返回：
        Position：BTC_USDT 持仓快照
    """
    return Position(
        contract="BTC_USDT",
        size=D(size),
        entry_price=D(entry),
        mark_price=D(100),
        liq_price=D(0),
        leverage=D(2),
        margin=D(margin),
        unrealised_pnl=D(0),
        margin_mode=mode,
    )


async def _zero_daily() -> DailyStats:
    """返回无盈亏、无下单计数的当日统计。

    参数：无

    返回：
        DailyStats：零值统计
    """
    return DailyStats(realized_pnl=D(0), orders_today=0)


async def _env(tmp_path, *, gateway: MockGateway | None = None) -> SimpleNamespace:
    """创建带 SQLite 仓库和工具注册表的测试环境。

    参数：
        tmp_path: Path，pytest 临时目录
        gateway: MockGateway | None，可选自定义网关

    返回：
        SimpleNamespace：数据库、网关、依赖与注册表
    """
    db = Database()
    await db.open(tmp_path / "margin-order.db")
    selected = gateway or MockGateway(
        contracts={"BTC_USDT": _contract()},
        account=Account(available=D(1000), unrealised_pnl=D(0)),
    )
    repo = Repo(db)
    deps = ToolDeps(
        gateway=selected,
        risk_engine=RiskEngine(),
        risk_config=RiskConfig(),
        watchlist=["BTC_USDT"],
        repo=repo,
        candles=CandleCache(selected, ManualPriceSource()),
        triggers=TriggerManager(lambda trigger, price: None),
        indicator_service=None,
        daily_stats_fn=_zero_daily,
        mode="paper",
        round_id="margin-test",
    )
    return SimpleNamespace(db=db, gateway=selected, deps=deps, registry=ToolRegistry(deps))


def _open_args(**overrides) -> dict:
    """构造默认恰好承担权益 1% 计划风险的开多参数。

    参数：
        overrides: 关键字参数，覆盖默认工具字段

    返回：
        dict：margin_usdt=100、2 倍杠杆、止损 95 的参数
    """
    return {
        "contract": "BTC_USDT",
        "side": "long",
        "margin_usdt": 100,
        "leverage": 2,
        "stop_loss_price": 95,
    } | overrides


async def test_margin_order_calculates_internal_size_and_one_percent_boundary(tmp_path):
    """校验保证金乘杠杆换算内部张数，整仓风险恰好 1% 时放行。

    参数：
        tmp_path: Path，pytest 临时目录

    返回：
        None，断言实际 2 张、逐仓 2 倍与完整回显
    """
    env = await _env(tmp_path)
    try:
        outcome = await env.registry.execute("place_order", _open_args())
        assert outcome.risk_verdict == "allow", outcome.text
        assert env.gateway.placed[-1].size == D(2)
        assert env.gateway.positions["BTC_USDT"].margin_mode == "isolated"
        assert "请求保证金 100" in outcome.text
        assert "实际名义仓位 200" in outcome.text
        assert "计划止损估算 10" in outcome.text
        assert "权益占比 1.0000%" in outcome.text
    finally:
        await env.db.close()


async def test_margin_order_over_one_percent_is_denied_without_placing(tmp_path):
    """校验计划止损超过权益 1% 时硬拒绝且不调用网关下单。

    参数：
        tmp_path: Path，pytest 临时目录

    返回：
        None，断言风险拒绝且 placed 为空
    """
    env = await _env(tmp_path)
    try:
        outcome = await env.registry.execute("place_order", _open_args(stop_loss_price=94))
        assert outcome.risk_verdict == "deny"
        assert "计划止损估算" in outcome.text
        assert env.gateway.placed == []
    finally:
        await env.db.close()


async def test_size_argument_is_rejected_and_not_in_schema(tmp_path):
    """校验 LLM 既看不到 size 字段，也不能手工绕过保证金换算。

    参数：
        tmp_path: Path，pytest 临时目录

    返回：
        None，断言 schema 无 size 且执行端返回参数错误
    """
    env = await _env(tmp_path)
    try:
        schema = next(item for item in env.registry.schemas() if item["name"] == "place_order")
        assert "size" not in schema["parameters"]["properties"]
        outcome = await env.registry.execute("place_order", _open_args(size=1))
        assert "不接受 size" in outcome.text
        assert env.gateway.placed == []
    finally:
        await env.db.close()


async def test_deprecated_execution_fields_cannot_be_silently_treated_as_exposure(tmp_path):
    """校验旧执行字段不会被静默忽略后变成新增敞口。

    参数：
        tmp_path: Path，pytest 临时目录

    返回：
        None，断言 reduce_only 与 margin_mode 均在网关写入前被拒绝
    """
    env = await _env(tmp_path)
    try:
        for field, value in (("reduce_only", True), ("margin_mode", "cross")):
            outcome = await env.registry.execute("place_order", _open_args(**{field: value}))
            assert "不接受这些旧执行字段" in outcome.text
        assert env.gateway.placed == []
    finally:
        await env.db.close()


async def test_available_balance_includes_actual_margin_and_estimated_fee(tmp_path):
    """校验实际保证金加预计手续费超过可用余额时拒绝。

    参数：
        tmp_path: Path，pytest 临时目录

    返回：
        None，断言余额错误且不下单
    """
    env = await _env(tmp_path)
    try:
        outcome = await env.registry.execute(
            "place_order", _open_args(margin_usdt=1000, leverage=1, stop_loss_price=99)
        )
        assert "预计手续费超过当前可用余额" in outcome.text
        assert env.gateway.placed == []
    finally:
        await env.db.close()


async def test_pending_increase_blocks_second_exposure_order(tmp_path):
    """校验同合约已有未成交增仓单时拒绝第二张增仓单。

    参数：
        tmp_path: Path，pytest 临时目录

    返回：
        None，断言第一张限价单挂起、第二张被拦截
    """
    env = await _env(tmp_path)
    try:
        first = await env.registry.execute("place_order", _open_args(price=100))
        second = await env.registry.execute("place_order", _open_args(price=100))
        assert first.risk_verdict == "allow"
        assert second.risk_verdict == "deny"
        assert "已有未成交增仓订单" in second.text
        assert len(env.gateway.placed) == 1
    finally:
        await env.db.close()


async def test_reverse_requires_close_first(tmp_path):
    """校验已有空仓时不能直接提交开多反手订单。

    参数：
        tmp_path: Path，pytest 临时目录

    返回：
        None，断言反手被拒绝且网关未收到订单
    """
    gateway = MockGateway(
        contracts={"BTC_USDT": _contract()},
        account=Account(available=D(1000), unrealised_pnl=D(0)),
        positions={"BTC_USDT": _position(size="-1")},
    )
    env = await _env(tmp_path, gateway=gateway)
    try:
        outcome = await env.registry.execute("place_order", _open_args(margin_usdt=50))
        assert "先 close=true 平仓" in outcome.text
        assert gateway.placed == []
    finally:
        await env.db.close()


async def test_final_recheck_aborts_when_position_changes_during_validation(tmp_path):
    """校验两次锁内最终快照不一致时中止，不用旧持仓结果下单。

    参数：
        tmp_path: Path，pytest 临时目录

    返回：
        None，断言状态变化提示且没有下单
    """

    class ChangingGateway(MockGateway):
        """第二次持仓读取前注入一张外部新增持仓。"""

        reads = 0

        def list_positions(self) -> list[Position]:
            """第二次读取起返回新持仓。

            参数：无

            返回：
                list[Position]：首次为空，后续含一张多仓
            """
            self.reads += 1
            if self.reads >= 2:
                self.positions["BTC_USDT"] = _position(size="1")
            return super().list_positions()

    gateway = ChangingGateway(
        contracts={"BTC_USDT": _contract()},
        account=Account(available=D(1000), unrealised_pnl=D(0)),
    )
    env = await _env(tmp_path, gateway=gateway)
    try:
        outcome = await env.registry.execute("place_order", _open_args(stop_loss_price=97))
        assert outcome.risk_verdict == "deny"
        assert "校验期间" in outcome.text
        assert gateway.placed == []
    finally:
        await env.db.close()


async def test_final_recheck_uses_latest_volatile_market_snapshot(tmp_path):
    """校验连续合法标记价变化不会被误判为持仓竞态。

    参数：
        tmp_path: Path，pytest 临时目录

    返回：
        None，断言使用最终实时规格完成市价下单
    """

    class MovingMarketGateway(MockGateway):
        """每次规格读取都返回稍有变化的正常标记价。"""

        reads = 0

        def get_contract(self, contract: str) -> Contract:
            """返回持续更新的可交易规格。

            参数：
                contract: str，合约名

            返回：
                Contract：标记价逐次增加 0.1 的规格
            """
            self.reads += 1
            return self.contracts[contract].model_copy(
                update={"mark_price": D("100") + D(self.reads) / D(10)}
            )

    gateway = MovingMarketGateway(
        contracts={"BTC_USDT": _contract()},
        account=Account(available=D(1000), unrealised_pnl=D(0)),
    )
    env = await _env(tmp_path, gateway=gateway)
    try:
        outcome = await env.registry.execute(
            "place_order",
            _open_args(margin_usdt=110, leverage=1, stop_loss_price=99),
        )
        assert outcome.risk_verdict == "allow", outcome.text
        assert gateway.reads >= 2
        assert len(gateway.placed) == 1
    finally:
        await env.db.close()


async def test_same_direction_add_uses_projected_whole_position_average(tmp_path):
    """校验同向加仓按预计新均价计算原仓加新仓的整仓止损风险。

    参数：
        tmp_path: Path，pytest 临时目录

    返回：
        None，断言原仓 80、新仓 100 加权为 90，止损 85 的整仓风险为 10
    """
    gateway = MockGateway(
        contracts={"BTC_USDT": _contract()},
        account=Account(available=D(900), unrealised_pnl=D(0)),
        positions={"BTC_USDT": _position(size="1", entry="80", margin="100")},
    )
    env = await _env(tmp_path, gateway=gateway)
    try:
        outcome = await env.registry.execute(
            "place_order", _open_args(margin_usdt=50, leverage=2, stop_loss_price=85)
        )
        assert outcome.risk_verdict == "allow", outcome.text
        assert "计划止损估算 10" in outcome.text
        assert gateway.positions["BTC_USDT"].size == D(2)
        assert gateway.positions["BTC_USDT"].entry_price == D(90)
    finally:
        await env.db.close()


async def test_contract_query_failure_denies_new_exposure(tmp_path):
    """校验实时合约规格查询失败时拒绝新增敞口且不使用旧数据兜底。

    参数：
        tmp_path: Path，pytest 临时目录

    返回：
        None，断言风险拒绝与零下单
    """

    class BrokenContractGateway(MockGateway):
        """始终无法读取合约规格的网关。"""

        def get_contract(self, contract: str) -> Contract:
            """模拟官方合约查询失败。

            参数：
                contract: str，合约名

            返回：
                Contract：本实现不会返回

            异常：
                GatewayError：始终抛出网络失败
            """
            raise GatewayError("官方规格查询失败")

    gateway = BrokenContractGateway(
        contracts={"BTC_USDT": _contract()},
        account=Account(available=D(1000), unrealised_pnl=D(0)),
    )
    env = await _env(tmp_path, gateway=gateway)
    try:
        outcome = await env.registry.execute("place_order", _open_args())
        assert outcome.risk_verdict == "deny"
        assert "实时读取 Gate 合约规格" in outcome.text
        assert gateway.placed == []
    finally:
        await env.db.close()


async def test_delisting_contract_denies_new_exposure(tmp_path):
    """校验合约不可交易或正在下架时按硬风控拒绝新增敞口。

    参数：
        tmp_path: Path，pytest 临时目录

    返回：
        None，断言拒绝结果进入风控审计且不触达下单
    """
    contract = _contract().model_copy(update={"status": "delisting", "in_delisting": True})
    gateway = MockGateway(
        contracts={"BTC_USDT": contract},
        account=Account(available=D(1000), unrealised_pnl=D(0)),
    )
    env = await _env(tmp_path, gateway=gateway)
    try:
        outcome = await env.registry.execute("place_order", _open_args())
        assert outcome.risk_verdict == "deny"
        assert "不可交易或正在下架" in outcome.text
        assert gateway.placed == []
    finally:
        await env.db.close()


async def test_paper_open_refreshes_contract_before_sizing(tmp_path):
    """校验 paper 新增敞口使用公共刷新后的规格换算张数。

    参数：
        tmp_path: Path，pytest 临时目录

    返回：
        None，断言最终校验两次刷新且新乘数决定实际张数
    """

    class RefreshingPaperGateway(MockGateway):
        """用不同乘数模拟 paper 公共规格刷新的网关。"""

        def __init__(self):
            """初始化旧规格、账户与刷新计数。

            参数：无

            返回：
                None，就地初始化测试网关
            """
            super().__init__(
                contracts={"BTC_USDT": _contract()},
                account=Account(available=D(1000), unrealised_pnl=D(0)),
            )
            self.refresh_calls = 0

        def refresh_contract(self, contract: str) -> Contract:
            """返回乘数为 2 的最新规格并更新内存。

            参数：
                contract: str，合约名

            返回：
                Contract：刷新后的合约规格
            """
            self.refresh_calls += 1
            latest = _contract().model_copy(update={"quanto_multiplier": D(2)})
            self.contracts[contract] = latest
            return latest

    gateway = RefreshingPaperGateway()
    env = await _env(tmp_path, gateway=gateway)
    try:
        outcome = await env.registry.execute("place_order", _open_args(stop_loss_price=95))
        assert outcome.risk_verdict == "allow", outcome.text
        assert gateway.refresh_calls == 2
        assert gateway.placed[-1].size == D(1)
    finally:
        await env.db.close()


async def test_close_and_reduce_do_not_depend_on_contract_query(tmp_path):
    """校验官方规格临时不可用时，整仓平仓和部分减仓仍能降险。

    参数：
        tmp_path: Path，pytest 临时目录

    返回：
        None，断言先减半再整仓平仓均放行
    """

    class BrokenContractGateway(MockGateway):
        """合约规格查询失败但持仓和下单仍可用的网关。"""

        def get_contract(self, contract: str) -> Contract:
            """模拟官方规格查询失败。

            参数：
                contract: str，合约名

            返回：
                Contract：本实现不会返回

            异常：
                GatewayError：始终抛出网络失败
            """
            raise GatewayError("官方规格查询失败")

    gateway = BrokenContractGateway(
        contracts={"BTC_USDT": _contract()},
        account=Account(available=D(1000), unrealised_pnl=D(0)),
        positions={"BTC_USDT": _position(size="10")},
    )
    env = await _env(tmp_path, gateway=gateway)
    try:
        reduced = await env.registry.execute(
            "place_order", {"contract": "BTC_USDT", "reduce_pct": 0.5}
        )
        closed = await env.registry.execute("place_order", {"contract": "BTC_USDT", "close": True})
        assert reduced.risk_verdict == "allow", reduced.text
        assert closed.risk_verdict == "allow", closed.text
        assert gateway.positions["BTC_USDT"].size == 0
    finally:
        await env.db.close()


async def test_decimal_contract_integer_position_can_reduce_fractional_lot(tmp_path):
    """校验小数张合约即使当前持仓显示整数，也能按比例减出小数张。

    参数：
        tmp_path: Path，pytest 临时目录

    返回：
        None，断言 1 张持仓减半提交 -0.5 张只减仓单
    """
    gateway = MockGateway(
        contracts={"BTC_USDT": _contract(decimal_size=True, minimum="0.1")},
        account=Account(available=D(1000), unrealised_pnl=D(0)),
        positions={"BTC_USDT": _position(size="1")},
    )
    env = await _env(tmp_path, gateway=gateway)
    try:
        outcome = await env.registry.execute(
            "place_order", {"contract": "BTC_USDT", "reduce_pct": 0.5}
        )
        assert outcome.risk_verdict == "allow", outcome.text
        assert gateway.placed[-1].size == D("-0.5")
        assert gateway.placed[-1].reduce_only is True
    finally:
        await env.db.close()
