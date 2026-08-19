"""工具层测试：amend 过风控、声明杠杆真实生效、落库失败禁止重试、
close 单豁免价格偏离、reduce_only 不计入日下单数、orders.is_close 轻量迁移、
研报方向闸门（高置信反向开仓拦截与各路降级放行）、
place_order 布尔参数严格类型校验、声明杠杆下单失败的回滚与状态提示。"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from decimal import Decimal
from types import SimpleNamespace

from src.agent.tool_handlers import ToolDeps
from src.agent.tool_leverage import _recheck_prev_state
from src.agent.tool_trading import _resolve_leverage
from src.agent.tools import ToolRegistry
from src.config import ResearchConfig, RiskConfig
from src.gateway.base import Contract, GatewayError, OrderStateUnknown, Position
from src.gateway.mock import MockGateway
from src.market.candles import CandleCache, ManualPriceSource
from src.market.triggers import TriggerManager
from src.memory import Database, Repo
from src.risk.engine import RiskEngine
from src.risk.models import DailyStats


def _contract(name: str, quanto: str, mark: str) -> Contract:
    """构造一个测试用合约对象（限额、费率等固定，标记价可调）。

    参数：
        name: str，合约名（如 "BTC_USDT"）
        quanto: str，合约乘数（quanto_multiplier），字符串形式转 Decimal
        mark: str，标记价格，字符串形式转 Decimal

    返回：
        Contract：填充了固定交易参数与费率的合约对象
    """
    return Contract(
        name=name,
        quanto_multiplier=Decimal(quanto),
        order_size_min=Decimal(1),
        order_size_max=Decimal("1000000"),
        order_price_round=Decimal("0.1"),
        enable_decimal=False,
        mark_price=Decimal(mark),
        funding_rate=Decimal("0.0001"),
        funding_interval=28800,
        maker_fee_rate=Decimal("0.0002"),
        taker_fee_rate=Decimal("0.0005"),
        status="trading",
        in_delisting=False,
    )


async def _zero_daily() -> DailyStats:
    """提供恒为零的当日统计，作为风控 daily_stats_fn 的默认实现。

    参数：无

    返回：
        DailyStats：已实现盈亏为 0、当日下单数为 0 的统计对象
    """
    return DailyStats(realized_pnl=Decimal(0), orders_today=0)


async def _make_tools(
    tmp_path, *, extra_contracts: tuple = (), research_config: ResearchConfig | None = None
) -> SimpleNamespace:
    """组装工具注册表（MockGateway + tmp_path SQLite）。

    参数：
        tmp_path: Path，pytest 提供的临时目录
        extra_contracts: tuple，除 BTC 外需注册的附加测试合约
        research_config: ResearchConfig | None，可选的研报方向闸门配置

    返回：
        SimpleNamespace，包含数据库、仓储、模拟网关、工具依赖和注册表的测试环境
    """
    db = Database()
    await db.open(tmp_path / "tools.db")
    repo = Repo(db)
    contracts = {"BTC_USDT": _contract("BTC_USDT", "0.001", "60000")}
    for name in extra_contracts:
        contracts[name] = _contract(name, "0.001", "60000")
    gateway = MockGateway(contracts=contracts)
    deps = ToolDeps(
        gateway=gateway,
        risk_engine=RiskEngine(),
        risk_config=RiskConfig(),
        watchlist=["BTC_USDT"],
        repo=repo,
        candles=CandleCache(gateway, ManualPriceSource()),
        triggers=TriggerManager(lambda t, p: None),
        indicator_service=None,
        daily_stats_fn=_zero_daily,
        research_config=research_config,
        mode="paper",
        round_id="r-test",
    )
    return SimpleNamespace(
        db=db, repo=repo, gateway=gateway, deps=deps, registry=ToolRegistry(deps)
    )


async def _open_limit_order(env: SimpleNamespace, size: int = 1, price: int = 59000) -> str:
    """下一笔限价挂单（保持 open），返回该挂单号。

    参数：
        env: SimpleNamespace，包含测试依赖的环境对象
        size: int，订单张数
        price: int，订单价格

    返回：
        str，模拟网关中新建且保持 open 状态的限价单编号
    """
    pos = env.gateway.positions.get("BTC_USDT")
    opens = (
        pos is None or pos.size == 0 or (pos.size > 0) == (size > 0) or abs(size) > abs(pos.size)
    )
    args = {"contract": "BTC_USDT", "size": size, "price": price}
    if opens:
        args["stop_loss_price"] = 58000 if size > 0 else 62000
    out = await env.registry.execute("place_order", args)
    assert out.risk_verdict == "allow", out.text
    return next(o.id for o in env.gateway.orders.values() if o.status == "open")


# ---------- amend_order 必须先过风控 ----------


async def test_amend_order_over_position_limit_denied(tmp_path):
    """校验改单后名义价值超过单仓上限时被风控拒绝，且原挂单不变。

    参数：
        tmp_path: Path，pytest 临时目录夹具，测试数据库落在其中

    返回：
        None，断言 risk_verdict 为 deny、提示含"单仓"且挂单剩余数量仍为 1
    """
    env = await _make_tools(tmp_path)
    try:
        order_id = await _open_limit_order(env)
        out = await env.registry.execute(
            "amend_order", {"contract": "BTC_USDT", "order_id": order_id, "size": 100}
        )  # 改后名义价值 6000 > 单仓上限 3000
        assert out.risk_verdict == "deny" and "单仓" in out.text
        assert env.gateway.orders[order_id].left == Decimal(1)  # 改单未生效
    finally:
        await env.db.close()


async def test_amend_order_price_deviation_denied(tmp_path):
    """校验改单价格偏离标记价超过阈值时被风控拒绝，且原挂单不变。

    参数：
        tmp_path: Path，pytest 临时目录夹具，测试数据库落在其中

    返回：
        None，断言 risk_verdict 为 deny、提示含"偏离"且挂单剩余数量仍为 1
    """
    env = await _make_tools(tmp_path)
    try:
        order_id = await _open_limit_order(env)
        out = await env.registry.execute(
            "amend_order", {"contract": "BTC_USDT", "order_id": order_id, "price": 70000}
        )  # 偏离 16.7% > 2%
        assert out.risk_verdict == "deny" and "偏离" in out.text
        assert env.gateway.orders[order_id].left == Decimal(1)
    finally:
        await env.db.close()


async def test_amend_order_non_watchlist_denied(tmp_path):
    """校验对白名单外合约改单直接被风控拒绝。

    参数：
        tmp_path: Path，pytest 临时目录夹具，测试数据库落在其中

    返回：
        None，断言 risk_verdict 为 deny 且提示含"白名单"
    """
    env = await _make_tools(tmp_path, extra_contracts=("DOGE_USDT",))
    try:
        out = await env.registry.execute(
            "amend_order", {"contract": "DOGE_USDT", "order_id": "x", "size": 1}
        )
        assert out.risk_verdict == "deny" and "白名单" in out.text
    finally:
        await env.db.close()


async def test_amend_order_allowed_and_persisted(tmp_path):
    """校验合规改单放行、网关生效，且落库为更新原行而非新增记录。

    参数：
        tmp_path: Path，pytest 临时目录夹具，测试数据库落在其中

    返回：
        None，断言改单后挂单剩余数量变为 2、库中仅一条订单记录且价格/数量已更新
    """
    env = await _make_tools(tmp_path)
    try:
        order_id = await _open_limit_order(env)
        out = await env.registry.execute(
            "amend_order",
            {"contract": "BTC_USDT", "order_id": order_id, "price": 59500, "size": 2},
        )
        assert out.risk_verdict == "allow", out.text
        assert env.gateway.orders[order_id].left == Decimal(2)
        rows = await env.repo.list_orders("r-test")
        assert len(rows) == 1  # 更新原行，不新增（避免 orders_today 重复计数）
        assert rows[0].price == Decimal("59500") and rows[0].side_size == Decimal(2)
    finally:
        await env.db.close()


async def test_amend_reduce_direction_exempt_kill_switch(tmp_path):
    """校验 kill_switch 开启时减仓方向改单豁免，加仓方向改单照拒。

    参数：
        tmp_path: Path，pytest 临时目录夹具，测试数据库落在其中

    返回：
        None，断言减仓改单 allow、反向加仓改单 deny 且提示含 "kill_switch"
    """
    env = await _make_tools(tmp_path)
    try:
        await env.registry.execute(
            "place_order", {"contract": "BTC_USDT", "size": 1, "stop_loss_price": 58000}
        )  # 持多仓
        order_id = await _open_limit_order(env, size=-1, price=59500)  # 限价卖单挂出
        env.deps.risk_config.kill_switch = True
        out = await env.registry.execute(
            "amend_order",
            {"contract": "BTC_USDT", "order_id": order_id, "size": -1, "price": 59600},
        )
        assert out.risk_verdict == "allow", out.text  # 减仓方向豁免 kill_switch（同 place）
        out2 = await env.registry.execute(
            "amend_order",
            {"contract": "BTC_USDT", "order_id": order_id, "size": 2, "price": 59600},
        )
        assert out2.risk_verdict == "deny" and "kill_switch" in out2.text  # 加仓方向照拒
    finally:
        await env.db.close()


async def test_amend_flip_exceeds_position_not_exempt(tmp_path):
    """反向改单数量超过持仓 = 翻仓（新敞口），不得豁免 kill_switch（回归 #翻仓豁免洞）。

    参数：
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，通过断言验证上述行为，无返回值
    """
    env = await _make_tools(tmp_path)
    try:
        await env.registry.execute(
            "place_order", {"contract": "BTC_USDT", "size": 1, "stop_loss_price": 58000}
        )  # 持多仓 1
        order_id = await _open_limit_order(env, size=-1, price=59500)  # 反向限价卖单挂出
        env.deps.risk_config.kill_switch = True
        out = await env.registry.execute(
            "amend_order",
            {"contract": "BTC_USDT", "order_id": order_id, "size": -2, "price": 59600},
        )  # 反向改到 -2 > 持仓 1：翻仓成空头新敞口
        assert out.risk_verdict == "deny" and "kill_switch" in out.text
        assert env.gateway.orders[order_id].left == Decimal(1)  # 改单未生效
    finally:
        await env.db.close()


# ---------- place_order 声明杠杆真实生效 ----------


async def test_place_order_declared_leverage_over_limit_denied(tmp_path):
    """校验声明杠杆超过上限时下单被拒，且订单未提交到网关。

    参数：
        tmp_path: Path，pytest 临时目录夹具，测试数据库落在其中

    返回：
        None，断言 risk_verdict 为 deny、提示含"超过上限"且网关无任何下单记录
    """
    env = await _make_tools(tmp_path)
    try:
        out = await env.registry.execute(
            "place_order",
            {"contract": "BTC_USDT", "size": 1, "leverage": 10, "stop_loss_price": 58000},
        )
        assert out.risk_verdict == "deny" and "超过上限" in out.text
        assert env.gateway.placed == []
    finally:
        await env.db.close()


async def test_place_order_declared_leverage_applied(tmp_path):
    """校验无持仓时声明杠杆在下单前真实设置到网关并反映到持仓上。

    参数：
        tmp_path: Path，pytest 临时目录夹具，测试数据库落在其中

    返回：
        None，断言 set_leverage 以声明杠杆被调用一次且持仓杠杆变为声明值
    """
    env = await _make_tools(tmp_path)
    try:
        spy = []
        orig = env.gateway.set_leverage

        def _spy(contract, leverage, margin_mode="isolated"):
            """记录 set_leverage 调用参数后转发给原实现。

            参数：
                contract: str，合约名
                leverage: int，杠杆倍数
                margin_mode: str，保证金模式，默认 "isolated"

            返回：
                原 set_leverage 的返回值
            """
            spy.append((contract, leverage, margin_mode))
            return orig(contract, leverage, margin_mode)

        env.gateway.set_leverage = _spy
        out = await env.registry.execute(
            "place_order",
            {"contract": "BTC_USDT", "size": 1, "leverage": 3, "stop_loss_price": 58000},
        )
        assert out.risk_verdict == "allow", out.text
        assert spy == [("BTC_USDT", 3, "isolated")]  # 无持仓：下单前先 set_leverage 生效
        assert env.gateway.positions["BTC_USDT"].leverage == Decimal(3)
    finally:
        await env.db.close()


# ---------- 下单成功但本地落库失败：禁止重试 ----------


async def test_place_order_local_save_failure_forbids_retry(tmp_path, monkeypatch):
    """验证交易所下单成功但本地保存失败时禁止盲目重试。

    参数：
        tmp_path: Path，pytest 提供的临时目录
        monkeypatch: pytest.MonkeyPatch，用于隔离并替换依赖或环境变量的 pytest 夹具

    返回：
        None，通过断言验证上述行为，无返回值
    """
    env = await _make_tools(tmp_path)
    try:

        async def _boom(**kwargs):
            """模拟依赖调用失败并抛出预设异常。

            参数：
                **kwargs: dict[str, object]，按名称传入的可选参数

            返回：
                None，实际不会返回（函数总是抛出异常）

            异常：
                RuntimeError，模拟本地数据库读写失败时抛出
            """
            raise RuntimeError("db down")

        monkeypatch.setattr(env.repo, "save_order", _boom)
        out = await env.registry.execute(
            "place_order", {"contract": "BTC_USDT", "size": 1, "stop_loss_price": 58000}
        )
        assert "禁止重试" in out.text
        assert "内部错误" not in out.text
        assert len(env.gateway.placed) == 1  # 订单已真实提交到网关
    finally:
        await env.db.close()


# ---------- reduce_only 平仓单不计入 orders_today + is_close 迁移 ----------


async def test_reduce_only_order_not_counted_in_orders_today(tmp_path):
    """验证只减仓订单不计入当日开仓订单数。

    参数：
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，通过断言验证上述行为，无返回值
    """
    env = await _make_tools(tmp_path)
    try:
        out = await env.registry.execute(
            "place_order", {"contract": "BTC_USDT", "size": 1, "stop_loss_price": 58000}
        )
        assert out.risk_verdict == "allow"
        out = await env.registry.execute(
            "place_order", {"contract": "BTC_USDT", "size": -1, "reduce_only": True}
        )
        assert out.risk_verdict == "allow", out.text
        stats = await env.repo.daily_stats("paper", 0.0)
        assert stats.orders_today == 1  # 只有开仓单计入
    finally:
        await env.db.close()


async def test_orders_is_close_column_migration(tmp_path):
    """验证旧订单表可迁移并补齐平仓标记列。

    参数：
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，通过断言验证上述行为，无返回值
    """
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE orders (id TEXT PRIMARY KEY, round_id TEXT NOT NULL, mode TEXT NOT NULL,"
        " contract TEXT NOT NULL, side_size TEXT NOT NULL, price TEXT NOT NULL DEFAULT '',"
        " tif TEXT NOT NULL DEFAULT '', text TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,"
        " finish_as TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL)"
    )
    conn.execute(
        "INSERT INTO orders(id,round_id,mode,contract,side_size,price,status,created_at)"
        " VALUES('o1','r','paper','BTC_USDT','0','','finished',1.0)"
    )
    conn.commit()
    conn.close()
    db = Database()
    await db.open(path)  # 幂等补列；历史 close 单（side_size='0'）回填 is_close=1
    try:
        repo = Repo(db)
        await repo.save_order(
            order_id="o2",
            round_id="r",
            mode="paper",
            contract="BTC_USDT",
            side_size=Decimal(1),
            is_close=True,
        )
        stats = await repo.daily_stats("paper", 0.0)
        assert stats.orders_today == 0  # 回填行与新平仓行都不计入
    finally:
        await db.close()


# ---------- close 单豁免价格偏离（其余规则照查） ----------


async def test_close_order_skips_price_deviation(tmp_path):
    """验证纯平仓订单不受开仓价格偏离限制。

    参数：
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，通过断言验证上述行为，无返回值
    """
    env = await _make_tools(tmp_path)
    try:
        await env.registry.execute(
            "place_order", {"contract": "BTC_USDT", "size": 1, "stop_loss_price": 58000}
        )  # 持多仓
        out = await env.registry.execute(
            "place_order", {"contract": "BTC_USDT", "close": True, "price": 100}
        )
        assert out.risk_verdict == "allow", out.text  # close 单 price 被网关忽略，不过偏离
        out2 = await env.registry.execute(
            "place_order", {"contract": "BTC_USDT", "size": 1, "price": 100, "stop_loss_price": 90}
        )
        assert out2.risk_verdict == "deny" and "偏离" in out2.text  # 开仓仍受偏离约束
    finally:
        await env.db.close()


# ---------- 研报方向闸门（高置信反向开仓拦截，降级一律放行） ----------

_GATE_ON = ResearchConfig(gate_enabled=True, gate_max_age_hours=13)
_OPEN_LONG = {"contract": "BTC_USDT", "size": 1, "stop_loss_price": 58000}


async def _save_report(
    env: SimpleNamespace,
    direction: str,
    confidence: str,
    *,
    contract: str = "BTC_USDT",
    basis_type: str = "混合",
    technical_confirmation: str = "确认",
    data_status: str = "完整",
) -> None:
    """落一份最新 v2 逐标的研报（created_at 为当前时刻）。

    参数：
        env: SimpleNamespace，包含测试依赖的环境对象
        direction: str，逐标的研报方向
        confidence: str，研报置信度
        contract: str，目标合约标识
        basis_type: str，研判依据类型
        technical_confirmation: str，技术面确认状态
        data_status: str，研报数据可用状态

    返回：
        None，执行上述模拟操作或副作用，无返回值
    """
    await env.repo.research.save_report_bundle(
        report_type="manual",
        summary="逐标的研报",
        cross_market_view="",
        global_risks_json="[]",
        raw_json="{}",
        round_id="r-research",
        asset_views=[
            {
                "contract": contract,
                "direction": direction,
                "confidence": confidence,
                "horizon": "24h",
                "market_regime": "下跌趋势",
                "technical_confirmation": technical_confirmation,
                "basis_type": basis_type,
                "data_status": data_status,
                "evidence_json": "[]",
                "risks_json": "[]",
                "narrative": "",
                "market_context_json": "{}",
            }
        ],
    )


async def test_research_gate_high_confidence_blocks_counter_order(tmp_path):
    """验证高置信度研报会拦截反向开仓。

    参数：
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，通过断言验证上述行为，无返回值
    """
    env = await _make_tools(tmp_path, research_config=_GATE_ON)
    try:
        await _save_report(env, "偏空", "高")
        out = await env.registry.execute("place_order", _OPEN_LONG)
        assert out.risk_verdict == "deny" and "闸门" in out.text
        assert env.gateway.placed == []  # 被闸门硬拒，订单未到网关
    finally:
        await env.db.close()


async def test_research_gate_mid_confidence_allows(tmp_path):
    """验证中等置信度研报仅提示而不拦截下单。

    参数：
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，通过断言验证上述行为，无返回值
    """
    env = await _make_tools(tmp_path, research_config=_GATE_ON)
    try:
        await _save_report(env, "偏空", "中")
        out = await env.registry.execute("place_order", _OPEN_LONG)
        assert out.risk_verdict == "allow", out.text
    finally:
        await env.db.close()


async def test_research_gate_low_confidence_allows(tmp_path):
    """验证低置信度研报不拦截下单。

    参数：
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，通过断言验证上述行为，无返回值
    """
    env = await _make_tools(tmp_path, research_config=_GATE_ON)
    try:
        await _save_report(env, "偏空", "低")
        out = await env.registry.execute("place_order", _OPEN_LONG)
        assert out.risk_verdict == "allow", out.text
    finally:
        await env.db.close()


async def test_research_gate_expired_report_allows(tmp_path):
    """验证过期研报不参与方向拦截。

    参数：
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，通过断言验证上述行为，无返回值
    """
    env = await _make_tools(tmp_path, research_config=_GATE_ON)
    try:
        await _save_report(env, "偏空", "高")
        # 把研报创建时间改到 14 小时前（超过 13 小时有效期）
        await env.db.conn.execute(
            "UPDATE research_reports SET created_at = ?", (time.time() - 14 * 3600,)
        )
        await env.db.conn.commit()
        await env.db.conn.execute(
            "UPDATE research_asset_views SET created_at = ?", (time.time() - 14 * 3600,)
        )
        out = await env.registry.execute("place_order", _OPEN_LONG)
        assert out.risk_verdict == "allow", out.text
    finally:
        await env.db.close()


async def test_research_gate_disabled_allows(tmp_path):
    """验证关闭研报闸门后允许下单。

    参数：
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，通过断言验证上述行为，无返回值
    """
    env = await _make_tools(tmp_path, research_config=ResearchConfig(gate_enabled=False))
    try:
        await _save_report(env, "偏空", "高")
        out = await env.registry.execute("place_order", _OPEN_LONG)
        assert out.risk_verdict == "allow", out.text
    finally:
        await env.db.close()


async def test_research_gate_no_config_allows(tmp_path):
    # research_config=None（旧构造方式）：闸门关闭，高置信反向也放行
    """验证缺少研报闸门配置时保持兼容放行。

    参数：
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，通过断言验证上述行为，无返回值
    """
    env = await _make_tools(tmp_path)
    try:
        await _save_report(env, "偏空", "高")
        out = await env.registry.execute("place_order", _OPEN_LONG)
        assert out.risk_verdict == "allow", out.text
    finally:
        await env.db.close()


async def test_research_gate_no_report_allows(tmp_path):
    """验证没有研报时交易工具正常放行。

    参数：
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，通过断言验证上述行为，无返回值
    """
    env = await _make_tools(tmp_path, research_config=_GATE_ON)
    try:
        out = await env.registry.execute("place_order", _OPEN_LONG)
        assert out.risk_verdict == "allow", out.text
    finally:
        await env.db.close()


async def test_research_gate_latest_report_error_degrades(tmp_path, monkeypatch):
    """验证读取最新研报异常时闸门降级放行。

    参数：
        tmp_path: Path，pytest 提供的临时目录
        monkeypatch: pytest.MonkeyPatch，用于隔离并替换依赖或环境变量的 pytest 夹具

    返回：
        None，通过断言验证上述行为，无返回值
    """
    env = await _make_tools(tmp_path, research_config=_GATE_ON)
    try:

        async def _boom(contract: str):
            """模拟依赖调用失败并抛出预设异常。

            参数：
                contract: str，目标合约标识

            返回：
                None，实际不会返回（函数总是抛出异常）

            异常：
                RuntimeError，模拟本地数据库读写失败时抛出
            """
            raise RuntimeError("db down")

        monkeypatch.setattr(env.repo.research, "latest_asset_view", _boom)
        out = await env.registry.execute("place_order", _OPEN_LONG)
        assert out.risk_verdict == "allow", out.text  # 读取异常降级放行，不阻塞交易
    finally:
        await env.db.close()


async def test_research_gate_failed_report_is_ignored(tmp_path):
    """验证生成失败的研报不会触发交易拦截。

    参数：
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，通过断言验证上述行为，无返回值
    """
    env = await _make_tools(tmp_path, research_config=_GATE_ON)
    try:
        await env.repo.research.save_failed_report(
            report_type="manual",
            error="LLM 输出解析失败",
        )
        out = await env.registry.execute("place_order", _OPEN_LONG)
        assert out.risk_verdict == "allow", out.text
    finally:
        await env.db.close()


async def test_research_gate_btc_view_does_not_block_eth(tmp_path):
    """验证 BTC 研报观点不会串扰 ETH 下单。

    参数：
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，通过断言验证上述行为，无返回值
    """
    env = await _make_tools(tmp_path, extra_contracts=("ETH_USDT",), research_config=_GATE_ON)
    try:
        env.deps.watchlist.append("ETH_USDT")
        await _save_report(env, "偏空", "高", contract="BTC_USDT")
        out = await env.registry.execute(
            "place_order",
            {"contract": "ETH_USDT", "size": 1, "stop_loss_price": 58000},
        )
        assert out.risk_verdict == "allow", out.text
    finally:
        await env.db.close()


async def test_research_gate_structure_continuation_is_soft_reference(tmp_path):
    """验证结构延续型研报仅作为软参考。

    参数：
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，通过断言验证上述行为，无返回值
    """
    env = await _make_tools(tmp_path, research_config=_GATE_ON)
    try:
        await _save_report(env, "偏空", "高", basis_type="结构延续")
        out = await env.registry.execute("place_order", _OPEN_LONG)
        assert out.risk_verdict == "allow", out.text
    finally:
        await env.db.close()


async def test_research_gate_conflict_or_unavailable_data_allows(tmp_path):
    """验证证据冲突或数据不可用时闸门不做硬拦截。

    参数：
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，通过断言验证上述行为，无返回值
    """
    for technical, status in (("冲突", "完整"), ("确认", "不可用")):
        env = await _make_tools(tmp_path / f"{technical}-{status}", research_config=_GATE_ON)
        try:
            await _save_report(
                env, "偏空", "高", technical_confirmation=technical, data_status=status
            )
            out = await env.registry.execute("place_order", _OPEN_LONG)
            assert out.risk_verdict == "allow", out.text
        finally:
            await env.db.close()


# ---------- place_order 布尔参数严格类型校验（issue #69 回归） ----------


async def test_place_order_close_string_false_does_not_close_position(tmp_path):
    """验证字符串 "false" 不会被真值转换成全平仓动作。

    参数：
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，断言返回参数错误、持仓未被平掉且网关未收到平仓请求
    """
    env = await _make_tools(tmp_path)
    try:
        await env.registry.execute("place_order", _OPEN_LONG)  # 先持多仓
        assert env.gateway.positions["BTC_USDT"].size == 1
        out = await env.registry.execute(
            "place_order", {"contract": "BTC_USDT", "size": 1, "close": "false"}
        )
        assert "参数错误" in out.text and "close" in out.text
        assert env.gateway.positions["BTC_USDT"].size == 1  # 仓位未被平掉
        assert len(env.gateway.placed) == 1  # 只有开仓单进入网关
    finally:
        await env.db.close()


async def test_place_order_bool_params_reject_non_bool_types(tmp_path):
    """验证 close/reduce_only 只接受 JSON 布尔，显式 null、字符串、整数、数组、对象一律拒绝。

    参数：
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，断言各畸形取值均返回参数错误、网关未收到订单请求且杠杆未被调用
    """
    env = await _make_tools(tmp_path)
    try:
        for name in ("close", "reduce_only"):
            for bad in ("false", "true", "0", "", 0, 1, None, [], {}):
                args = {
                    "contract": "BTC_USDT",
                    "size": 1,
                    "stop_loss_price": 58000,
                    "leverage": 5,
                    name: bad,
                }
                out = await env.registry.execute("place_order", args)
                assert "参数错误" in out.text and name in out.text, f"{name}={bad!r} 未被拒绝"
        assert env.gateway.placed == []  # 下单未到达网关
        assert "BTC_USDT" not in env.gateway.positions  # 调杠杆未到达网关
    finally:
        await env.db.close()


async def test_place_order_bool_params_accept_explicit_bools(tmp_path):
    """验证显式 true/false 布尔值行为不变：close=false 正常开仓，close=true 正常全平。

    参数：
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，断言显式布尔入参下单成功且持仓变化符合预期
    """
    env = await _make_tools(tmp_path)
    try:
        out = await env.registry.execute(
            "place_order",
            {"contract": "BTC_USDT", "size": 1, "stop_loss_price": 58000, "close": False},
        )
        assert out.risk_verdict == "allow", out.text
        assert env.gateway.positions["BTC_USDT"].size == 1
        out = await env.registry.execute(
            "place_order", {"contract": "BTC_USDT", "close": True, "reduce_only": False}
        )
        assert out.risk_verdict == "allow", out.text
        assert env.gateway.positions["BTC_USDT"].size == 0
    finally:
        await env.db.close()


# ---------- 声明杠杆下单失败：回滚与状态提示（issue #70 回归） ----------


def _long_position(leverage: str, cross_limit: str = "5") -> Position:
    """构造 BTC_USDT 多仓持仓对象（leverage=0 表示全仓模式）。

    参数：
        leverage: str，持仓杠杆倍数的字符串形式，"0" 表示全仓（cross）
        cross_limit: str，全仓实际杠杆（cross_leverage_limit），仅 leverage="0" 时生效

    返回：
        Position：1 张多仓、固定价格与保证金的持仓对象
    """
    is_cross = leverage == "0"
    return Position(
        contract="BTC_USDT",
        size=Decimal(1),
        entry_price=Decimal(60000),
        mark_price=Decimal(60000),
        liq_price=Decimal(30000),
        leverage=Decimal(leverage),
        margin=Decimal(100),
        unrealised_pnl=Decimal(0),
        margin_mode="cross" if is_cross else "isolated",
        cross_leverage_limit=Decimal(cross_limit) if is_cross else None,
    )


def _spy_set_leverage(env: SimpleNamespace, spy: list) -> None:
    """把 env 网关的 set_leverage 替换为记录参数后转发原实现的探针。

    参数：
        env: SimpleNamespace，包含测试依赖的环境对象
        spy: list，用于收集 (contract, leverage, margin_mode) 调用记录的列表

    返回：
        None，就地替换 env.gateway.set_leverage 实例属性
    """
    orig = env.gateway.set_leverage

    def _record(contract, leverage, margin_mode="isolated"):
        """记录调用参数后转发给原 set_leverage。

        参数：
            contract: str，合约名
            leverage: int，杠杆倍数
            margin_mode: str，保证金模式，默认 "isolated"

        返回：
            原 set_leverage 的返回值
        """
        spy.append((contract, leverage, margin_mode))
        return orig(contract, leverage, margin_mode)

    env.gateway.set_leverage = _record


async def test_place_order_gateway_reject_rolls_back_leverage(tmp_path, monkeypatch):
    """验证声明杠杆后下单被交易所明确拒绝时，杠杆回滚到修改前状态。

    参数：
        tmp_path: Path，pytest 提供的临时目录
        monkeypatch: pytest.MonkeyPatch，用于替换网关下单方法注入失败

    返回：
        None，断言文本含回滚说明、杠杆恢复修改前值且 set_leverage 被调两次
    """
    env = await _make_tools(tmp_path)
    try:
        env.gateway.positions["BTC_USDT"] = _long_position("2")
        spy: list = []
        _spy_set_leverage(env, spy)

        def _reject(req):
            """模拟交易所明确拒绝下单。

            参数：
                req: OrderRequest，下单请求

            返回：
                None，实际不会返回（总是抛出异常）

            异常：
                GatewayError，模拟交易所明确拒绝（不会重单）
            """
            raise GatewayError("余额不足", label="INSUFFICIENT_BALANCE")

        monkeypatch.setattr(env.gateway, "place_order", _reject)
        out = await env.registry.execute(
            "place_order",
            {"contract": "BTC_USDT", "size": 1, "leverage": 3, "stop_loss_price": 58000},
        )
        assert "下单失败" in out.text and "已回滚" in out.text
        assert spy == [("BTC_USDT", 3, "isolated"), ("BTC_USDT", 2, "isolated")]
        assert env.gateway.positions["BTC_USDT"].leverage == Decimal(2)
    finally:
        await env.db.close()


async def test_place_order_risk_window_leverage_change_fails_closed(tmp_path):
    """验证风控 await 窗口内杠杆被并发修改时拒绝下单并触发风控锁（不用旧锚点回滚）。

    参数：
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，断言 deny+风控锁、未触达改杠杆、持仓保持并发修改后的 5x
    """
    env = await _make_tools(tmp_path)
    try:
        env.gateway.positions["BTC_USDT"] = _long_position("2")

        async def _hostile_daily() -> DailyStats:
            """在风控 await 窗口内把杠杆从 2x 并发改为 5x。

            参数：无

            返回：
                DailyStats：零统计（与默认实现一致）
            """
            env.gateway.positions["BTC_USDT"] = _long_position("5")
            return await _zero_daily()

        env.deps.daily_stats_fn = _hostile_daily
        spy: list = []
        _spy_set_leverage(env, spy)
        engaged = _wire_engage_spy(env)
        out = await env.registry.execute(
            "place_order",
            {"contract": "BTC_USDT", "size": 1, "leverage": 3, "stop_loss_price": 58000},
        )
        assert out.risk_verdict == "deny" and "风控锁" in out.text
        assert spy == []  # 未触达改杠杆
        assert len(engaged) == 1
        assert env.gateway.positions["BTC_USDT"].leverage == Decimal(5)  # 未被旧锚点覆盖
    finally:
        await env.db.close()


async def test_place_order_rollback_aborts_on_concurrent_change(tmp_path, monkeypatch):
    """验证下单失败时杠杆已被并发改动，回滚中止而不是用旧快照覆盖。

    参数：
        tmp_path: Path，pytest 提供的临时目录
        monkeypatch: pytest.MonkeyPatch，用于注入下单失败并模拟并发修改

    返回：
        None，断言回滚中止文案、风控锁触发、杠杆保持并发修改后的 9x
    """
    env = await _make_tools(tmp_path)
    try:
        env.gateway.positions["BTC_USDT"] = _long_position("2")

        def _reject_and_race(req):
            """模拟下单被拒的同时外部把杠杆从本次设置的 3x 并发改为 9x。

            参数：
                req: OrderRequest，下单请求

            返回：
                None，实际不会返回（总是抛出异常）

            异常：
                GatewayError，模拟交易所明确拒绝（不会重单）
            """
            env.gateway.positions["BTC_USDT"] = _long_position("9")
            raise GatewayError("余额不足", label="INSUFFICIENT_BALANCE")

        monkeypatch.setattr(env.gateway, "place_order", _reject_and_race)
        engaged = _wire_engage_spy(env)
        out = await env.registry.execute(
            "place_order",
            {"contract": "BTC_USDT", "size": 1, "leverage": 3, "stop_loss_price": 58000},
        )
        assert "回滚中止" in out.text and "风控锁" in out.text
        assert len(engaged) == 1
        assert env.gateway.positions["BTC_USDT"].leverage == Decimal(9)  # 并发修改未被覆盖
    finally:
        await env.db.close()


async def test_place_order_state_unknown_keeps_leverage(tmp_path, monkeypatch):
    """验证下单状态未知（可能已创建）时不回滚杠杆，仅提示人工核对。

    参数：
        tmp_path: Path，pytest 提供的临时目录
        monkeypatch: pytest.MonkeyPatch，用于替换网关下单方法注入超时异常

    返回：
        None，断言文本禁止盲目重试、杠杆保持本次修改值且 set_leverage 只调一次
    """
    env = await _make_tools(tmp_path)
    try:
        env.gateway.positions["BTC_USDT"] = _long_position("2")
        spy: list = []
        _spy_set_leverage(env, spy)

        def _timeout(req):
            """模拟下单超时且回查失败（订单状态未知）。

            参数：
                req: OrderRequest，下单请求

            返回：
                None，实际不会返回（总是抛出异常）

            异常：
                OrderStateUnknown，模拟订单可能已创建的不确定状态
            """
            raise OrderStateUnknown("下单超时且回查失败，订单状态未知")

        monkeypatch.setattr(env.gateway, "place_order", _timeout)
        out = await env.registry.execute(
            "place_order",
            {"contract": "BTC_USDT", "size": 1, "leverage": 3, "stop_loss_price": 58000},
        )
        assert "状态未知" in out.text and "禁止盲目重试" in out.text
        assert spy == [("BTC_USDT", 3, "isolated")]  # 不回滚
        assert env.gateway.positions["BTC_USDT"].leverage == Decimal(3)
        assert env.deps.risk_config.kill_switch is True  # 无回调时回退内存置位
    finally:
        await env.db.close()


async def test_place_order_margin_mode_follows_current_position(tmp_path):
    """验证未声明 margin_mode 时跟随当前持仓模式（全仓持仓不再被强带逐仓）。

    参数：
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，断言 set_leverage 收到 cross 模式且下单成功
    """
    env = await _make_tools(tmp_path)
    try:
        env.gateway.positions["BTC_USDT"] = _long_position("0")  # 全仓持仓
        spy: list = []
        _spy_set_leverage(env, spy)
        out = await env.registry.execute(
            "place_order",
            {"contract": "BTC_USDT", "size": 1, "leverage": 3, "stop_loss_price": 58000},
        )
        assert out.risk_verdict == "allow", out.text
        assert spy == [("BTC_USDT", 3, "cross")]
    finally:
        await env.db.close()


async def test_place_order_failure_without_prior_position_keeps_leverage(tmp_path, monkeypatch):
    """验证下单前无持仓时杠杆修改无法回滚，仅提示人工核对。

    参数：
        tmp_path: Path，pytest 提供的临时目录
        monkeypatch: pytest.MonkeyPatch，用于替换网关下单方法注入失败

    返回：
        None，断言文本提示人工核对、杠杆保持修改值且 set_leverage 只调一次
    """
    env = await _make_tools(tmp_path)
    try:
        spy: list = []
        _spy_set_leverage(env, spy)

        def _reject(req):
            """模拟交易所明确拒绝下单。

            参数：
                req: OrderRequest，下单请求

            返回：
                None，实际不会返回（总是抛出异常）

            异常：
                GatewayError，模拟交易所明确拒绝（不会重单）
            """
            raise GatewayError("张数低于最小限制", label="SIZE_TOO_SMALL")

        monkeypatch.setattr(env.gateway, "place_order", _reject)
        out = await env.registry.execute(
            "place_order",
            {"contract": "BTC_USDT", "size": 1, "leverage": 3, "stop_loss_price": 58000},
        )
        assert "下单失败" in out.text and "人工核对" in out.text
        assert spy == [("BTC_USDT", 3, "isolated")]  # 无先验状态，不回滚
    finally:
        await env.db.close()


# ---------- 杠杆状态可信度：全仓字段、核验与风控锁（issue #70 评审回归） ----------


def _wire_engage_spy(env: SimpleNamespace) -> list:
    """注入风控锁回调探针：记录触发原因并同步置位 kill_switch（模拟决策循环行为）。

    参数：
        env: SimpleNamespace，包含测试依赖的环境对象

    返回：
        list：触发原因收集列表，每次回调追加一条 reason
    """
    engaged: list = []

    def _engage(reason: str) -> None:
        """记录触发原因并置位风控锁。

        参数：
            reason: str，触发风控锁的原因描述

        返回：
            None，就地追加 engaged 列表并置位 env.deps.risk_config.kill_switch
        """
        engaged.append(reason)
        env.deps.risk_config.kill_switch = True

    env.deps.engage_kill_switch = _engage
    return engaged


async def test_place_order_rollback_restores_cross_leverage_limit(tmp_path, monkeypatch):
    """验证全仓 5x 临时改 3x 下单失败后，回滚恢复全仓 5x 而非固定 1x。

    参数：
        tmp_path: Path，pytest 提供的临时目录
        monkeypatch: pytest.MonkeyPatch，用于替换网关下单方法注入失败

    返回：
        None，断言回滚目标为 (5, cross)、持仓 cross_leverage_limit 恢复为 5
    """
    env = await _make_tools(tmp_path)
    try:
        env.gateway.positions["BTC_USDT"] = _long_position("0", "5")  # 全仓 5x
        spy: list = []
        _spy_set_leverage(env, spy)

        def _reject(req):
            """模拟交易所明确拒绝下单。

            参数：
                req: OrderRequest，下单请求

            返回：
                None，实际不会返回（总是抛出异常）

            异常：
                GatewayError，模拟交易所明确拒绝（不会重单）
            """
            raise GatewayError("余额不足", label="INSUFFICIENT_BALANCE")

        monkeypatch.setattr(env.gateway, "place_order", _reject)
        out = await env.registry.execute(
            "place_order",
            {"contract": "BTC_USDT", "size": 1, "leverage": 3, "stop_loss_price": 58000},
        )
        assert "已回滚至 5（cross）" in out.text
        assert spy == [("BTC_USDT", 3, "cross"), ("BTC_USDT", 5, "cross")]
        pos = env.gateway.positions["BTC_USDT"]
        assert pos.leverage == Decimal(0) and pos.cross_leverage_limit == Decimal(5)
    finally:
        await env.db.close()


async def test_place_order_skips_set_leverage_when_target_equals_current(tmp_path):
    """验证目标杠杆与保证金模式和当前持仓一致时，不调用交易所 set_leverage。

    参数：
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，断言 set_leverage 探针无调用记录且下单成功
    """
    env = await _make_tools(tmp_path)
    try:
        env.gateway.positions["BTC_USDT"] = _long_position("3")  # 逐仓 3x
        spy: list = []
        _spy_set_leverage(env, spy)
        out = await env.registry.execute(
            "place_order",
            {"contract": "BTC_USDT", "size": 1, "leverage": 3, "stop_loss_price": 58000},
        )
        assert out.risk_verdict == "allow", out.text
        assert spy == []  # 目标等于现状：不触达交易所
    finally:
        await env.db.close()


async def test_place_order_rollback_failure_engages_kill_switch(tmp_path, monkeypatch):
    """验证回滚失败触发风控锁，且下一笔开仓被风控拒绝。

    参数：
        tmp_path: Path，pytest 提供的临时目录
        monkeypatch: pytest.MonkeyPatch，用于注入下单拒绝与回滚失败

    返回：
        None，断言文本含风控锁提示、回调被触发、后续开仓返回 deny
    """
    env = await _make_tools(tmp_path)
    try:
        env.gateway.positions["BTC_USDT"] = _long_position("2")
        engaged = _wire_engage_spy(env)
        orig_set = env.gateway.set_leverage
        calls = {"n": 0}

        def _flaky_set(contract, leverage, margin_mode="isolated"):
            """首次正常设置，第二次（回滚）抛网关错误。

            参数：
                contract: str，合约名
                leverage: int，杠杆倍数
                margin_mode: str，保证金模式

            返回：
                首次调用返回原 set_leverage 的结果

            异常：
                GatewayError，第二次及以后调用抛出（模拟回滚失败）
            """
            calls["n"] += 1
            if calls["n"] == 1:
                return orig_set(contract, leverage, margin_mode)
            raise GatewayError("回滚请求失败", label="REQUEST_FAILED")

        state = {"reject": True}
        orig_place = env.gateway.place_order

        def _maybe_reject(req):
            """按开关拒绝或转发下单请求。

            参数：
                req: OrderRequest，下单请求

            返回：
                开关关闭时返回原 place_order 的结果

            异常：
                GatewayError，开关打开时抛出（模拟交易所明确拒绝）
            """
            if state["reject"]:
                raise GatewayError("余额不足", label="INSUFFICIENT_BALANCE")
            return orig_place(req)

        monkeypatch.setattr(env.gateway, "set_leverage", _flaky_set)
        monkeypatch.setattr(env.gateway, "place_order", _maybe_reject)
        out = await env.registry.execute(
            "place_order",
            {"contract": "BTC_USDT", "size": 1, "leverage": 3, "stop_loss_price": 58000},
        )
        assert "回滚失败" in out.text and "风控锁" in out.text
        assert len(engaged) == 1
        state["reject"] = False
        out2 = await env.registry.execute(
            "place_order",
            {"contract": "BTC_USDT", "size": 1, "leverage": 3, "stop_loss_price": 58000},
        )
        assert out2.risk_verdict == "deny" and "kill_switch" in out2.text
    finally:
        await env.db.close()


async def test_place_order_state_unknown_engages_kill_switch(tmp_path, monkeypatch):
    """验证下单状态未知时保持杠杆并触发风控锁。

    参数：
        tmp_path: Path，pytest 提供的临时目录
        monkeypatch: pytest.MonkeyPatch，用于替换网关下单方法注入超时异常

    返回：
        None，断言文本含风控锁提示、回调被触发、杠杆保持本次修改值
    """
    env = await _make_tools(tmp_path)
    try:
        env.gateway.positions["BTC_USDT"] = _long_position("2")
        engaged = _wire_engage_spy(env)

        def _timeout(req):
            """模拟下单超时且回查失败（订单状态未知）。

            参数：
                req: OrderRequest，下单请求

            返回：
                None，实际不会返回（总是抛出异常）

            异常：
                OrderStateUnknown，模拟订单可能已创建的不确定状态
            """
            raise OrderStateUnknown("下单超时且回查失败，订单状态未知")

        monkeypatch.setattr(env.gateway, "place_order", _timeout)
        out = await env.registry.execute(
            "place_order",
            {"contract": "BTC_USDT", "size": 1, "leverage": 3, "stop_loss_price": 58000},
        )
        assert "状态未知" in out.text and "禁止盲目重试" in out.text and "风控锁" in out.text
        assert len(engaged) == 1
        assert env.gateway.positions["BTC_USDT"].leverage == Decimal(3)
    finally:
        await env.db.close()


async def test_place_order_leverage_unknown_but_applied_reconciles(tmp_path, monkeypatch):
    """验证调杠杆报错但实际已生效时，对账确认后继续完成下单。

    参数：
        tmp_path: Path，pytest 提供的临时目录
        monkeypatch: pytest.MonkeyPatch，用于注入"远端已生效但本地收到异常"的调杠杆

    返回：
        None，断言对账后订单正常提交成功
    """
    env = await _make_tools(tmp_path)
    try:
        env.gateway.positions["BTC_USDT"] = _long_position("2")
        orig_set = env.gateway.set_leverage

        def _applied_then_error(contract, leverage, margin_mode="isolated"):
            """先实际修改杠杆再抛异常（模拟响应途中断连）。

            参数：
                contract: str，合约名
                leverage: int，杠杆倍数
                margin_mode: str，保证金模式

            返回：
                None，实际不会返回（总是抛出异常）

            异常：
                GatewayError，模拟远端已生效但本地未收到响应
            """
            orig_set(contract, leverage, margin_mode)
            raise GatewayError("连接中断，结果未知", label="REQUEST_TIMEOUT")

        monkeypatch.setattr(env.gateway, "set_leverage", _applied_then_error)
        out = await env.registry.execute(
            "place_order",
            {"contract": "BTC_USDT", "size": 1, "leverage": 3, "stop_loss_price": 58000},
        )
        assert "下单成功" in out.text, out.text
    finally:
        await env.db.close()


async def test_place_order_leverage_unknown_not_applied_aborts(tmp_path, monkeypatch):
    """验证调杠杆报错且经延迟复核确认未生效时，安全放弃下单（订单未提交）。

    参数：
        tmp_path: Path，pytest 提供的临时目录
        monkeypatch: pytest.MonkeyPatch，用于注入未生效的调杠杆异常

    返回：
        None，断言文本提示未生效、下单未被调用、杠杆保持修改前值
    """
    monkeypatch.setattr("src.agent.tool_leverage._UNKNOWN_SETTLE_DELAY_S", 0)
    env = await _make_tools(tmp_path)
    try:
        env.gateway.positions["BTC_USDT"] = _long_position("2")

        def _not_applied(contract, leverage, margin_mode="isolated"):
            """直接抛异常且不修改任何状态（模拟请求未到达交易所）。

            参数：
                contract: str，合约名
                leverage: int，杠杆倍数
                margin_mode: str，保证金模式

            返回：
                None，实际不会返回（总是抛出异常）

            异常：
                GatewayError，模拟调杠杆请求未生效
            """
            raise GatewayError("连接失败", label="REQUEST_FAILED")

        placed: list = []
        orig_place = env.gateway.place_order

        def _place_spy(req):
            """记录下单请求后转发原实现。

            参数：
                req: OrderRequest，下单请求

            返回：
                原 place_order 的返回值
            """
            placed.append(req)
            return orig_place(req)

        monkeypatch.setattr(env.gateway, "set_leverage", _not_applied)
        monkeypatch.setattr(env.gateway, "place_order", _place_spy)
        out = await env.registry.execute(
            "place_order",
            {"contract": "BTC_USDT", "size": 1, "leverage": 3, "stop_loss_price": 58000},
        )
        assert "未生效" in out.text and "订单未提交" in out.text
        assert placed == []
        assert env.gateway.positions["BTC_USDT"].leverage == Decimal(2)
    finally:
        await env.db.close()


async def test_place_order_leverage_unknown_delayed_commit_locks(tmp_path, monkeypatch):
    """验证结果未知的调杠杆在延迟复核窗口内迟到提交时，必须保持锁定而非误判未生效。

    参数：
        tmp_path: Path，pytest 提供的临时目录
        monkeypatch: pytest.MonkeyPatch，用于注入超时异常与模拟远端迟到提交

    返回：
        None，断言触发风控锁、订单未提交、绝不按"安全未生效"放行
    """
    monkeypatch.setattr("src.agent.tool_leverage._UNKNOWN_SETTLE_DELAY_S", 0)
    env = await _make_tools(tmp_path)
    try:
        env.gateway.positions["BTC_USDT"] = _long_position("2")
        engaged = _wire_engage_spy(env)
        raised = {"v": False}
        reads_after_raise = {"n": 0}
        orig_list = env.gateway.list_positions

        def _timeout_set(contract, leverage, margin_mode="isolated"):
            """模拟请求已发出但客户端读超时（结果未知，远端可能迟到提交）。

            参数：
                contract: str，合约名
                leverage: int，杠杆倍数
                margin_mode: str，保证金模式

            返回：
                None，实际不会返回（总是抛出异常）

            异常：
                TimeoutError，模拟读超时（非 GatewayError，属结果未知）
            """
            raised["v"] = True
            raise TimeoutError("read timeout")

        def _list_with_delayed_commit():
            """异常后的第二次读取起模拟远端迟到提交（杠杆被改成 3x）。

            参数：无

            返回：
                list[Position]：持仓快照；第二次起返回迟到提交后的状态
            """
            if raised["v"]:
                reads_after_raise["n"] += 1
                if reads_after_raise["n"] >= 2:
                    env.gateway.positions["BTC_USDT"].leverage = Decimal(3)
            return orig_list()

        placed: list = []
        orig_place = env.gateway.place_order

        def _place_spy(req):
            """记录下单请求后转发原实现。

            参数：
                req: OrderRequest，下单请求

            返回：
                原 place_order 的返回值
            """
            placed.append(req)
            return orig_place(req)

        monkeypatch.setattr(env.gateway, "set_leverage", _timeout_set)
        monkeypatch.setattr(env.gateway, "list_positions", _list_with_delayed_commit)
        monkeypatch.setattr(env.gateway, "place_order", _place_spy)
        out = await env.registry.execute(
            "place_order",
            {"contract": "BTC_USDT", "size": 1, "leverage": 3, "stop_loss_price": 58000},
        )
        assert "风控锁" in out.text and "未生效" not in out.text, out.text
        assert placed == []
        assert len(engaged) == 1
        assert env.deps.risk_config.kill_switch is True
    finally:
        await env.db.close()


async def test_place_order_leverage_unknown_stable_not_applied_aborts(tmp_path, monkeypatch):
    """验证结果未知的调杠杆经延迟复核全程稳定为旧值时，才安全宣告未生效。

    参数：
        tmp_path: Path，pytest 提供的临时目录
        monkeypatch: pytest.MonkeyPatch，用于注入超时异常（非 GatewayError）

    返回：
        None，断言文本提示未生效、不触发风控锁、订单未提交
    """
    monkeypatch.setattr("src.agent.tool_leverage._UNKNOWN_SETTLE_DELAY_S", 0)
    env = await _make_tools(tmp_path)
    try:
        env.gateway.positions["BTC_USDT"] = _long_position("2")

        def _timeout_set(contract, leverage, margin_mode="isolated"):
            """模拟读超时且远端始终未提交（状态全程稳定）。

            参数：
                contract: str，合约名
                leverage: int，杠杆倍数
                margin_mode: str，保证金模式

            返回：
                None，实际不会返回（总是抛出异常）

            异常：
                ConnectionError，模拟连接中断（非 GatewayError，属结果未知）
            """
            raise ConnectionError("connection reset")

        placed: list = []
        orig_place = env.gateway.place_order

        def _place_spy(req):
            """记录下单请求后转发原实现。

            参数：
                req: OrderRequest，下单请求

            返回：
                原 place_order 的返回值
            """
            placed.append(req)
            return orig_place(req)

        monkeypatch.setattr(env.gateway, "set_leverage", _timeout_set)
        monkeypatch.setattr(env.gateway, "place_order", _place_spy)
        out = await env.registry.execute(
            "place_order",
            {"contract": "BTC_USDT", "size": 1, "leverage": 3, "stop_loss_price": 58000},
        )
        assert "未生效" in out.text and "订单未提交" in out.text and "风控锁" not in out.text
        assert placed == []
        assert env.deps.risk_config.kill_switch is not True
        assert env.gateway.positions["BTC_USDT"].leverage == Decimal(2)
    finally:
        await env.db.close()


async def test_place_order_leverage_gateway_error_timeout_delayed_commit_locks(
    tmp_path, monkeypatch
):
    """验证 GatewayError(REQUEST_TIMEOUT) 包装的调杠杆结果未知时，迟到提交必须锁定而非误判未生效。

    GatewayError 是网关层统一包装（超时/5xx 同属该类别），不能按"明确拒绝"直接
    宣告未生效；首次读到旧值后必须进入延迟复核，复核窗口内观察到迟到提交
    （杠杆变为目标值）时触发风控锁。

    参数：
        tmp_path: Path，pytest 提供的临时目录
        monkeypatch: pytest.MonkeyPatch，用于注入超时异常与模拟远端迟到提交

    返回：
        None，断言触发风控锁、订单未提交、绝不按"安全未生效"放行
    """
    monkeypatch.setattr("src.agent.tool_leverage._UNKNOWN_SETTLE_DELAY_S", 0)
    env = await _make_tools(tmp_path)
    try:
        env.gateway.positions["BTC_USDT"] = _long_position("2")
        engaged = _wire_engage_spy(env)
        raised = {"v": False}
        reads_after_raise = {"n": 0}
        orig_list = env.gateway.list_positions

        def _timeout_set(contract, leverage, margin_mode="isolated"):
            """模拟请求已发出但客户端收到网关包装的超时错误（结果未知，远端可能迟到提交）。

            参数：
                contract: str，合约名
                leverage: int，杠杆倍数
                margin_mode: str，保证金模式

            返回：
                None，实际不会返回（总是抛出异常）

            异常：
                GatewayError，模拟被网关统一包装的超时错误（结果未知）
            """
            raised["v"] = True
            raise GatewayError("连接中断，结果未知", label="REQUEST_TIMEOUT")

        def _list_with_delayed_commit():
            """异常后的第二次读取起模拟远端迟到提交（杠杆被改成 3x）。

            参数：无

            返回：
                list[Position]：持仓快照；第二次起返回迟到提交后的状态
            """
            if raised["v"]:
                reads_after_raise["n"] += 1
                if reads_after_raise["n"] >= 2:
                    env.gateway.positions["BTC_USDT"].leverage = Decimal(3)
            return orig_list()

        placed: list = []
        orig_place = env.gateway.place_order

        def _place_spy(req):
            """记录下单请求后转发原实现。

            参数：
                req: OrderRequest，下单请求

            返回：
                原 place_order 的返回值
            """
            placed.append(req)
            return orig_place(req)

        monkeypatch.setattr(env.gateway, "set_leverage", _timeout_set)
        monkeypatch.setattr(env.gateway, "list_positions", _list_with_delayed_commit)
        monkeypatch.setattr(env.gateway, "place_order", _place_spy)
        out = await env.registry.execute(
            "place_order",
            {"contract": "BTC_USDT", "size": 1, "leverage": 3, "stop_loss_price": 58000},
        )
        assert "风控锁" in out.text and "未生效" not in out.text, out.text
        assert placed == []
        assert len(engaged) == 1
        assert env.deps.risk_config.kill_switch is True
    finally:
        await env.db.close()


async def test_place_order_inherited_leverage_concurrent_change_locks(tmp_path, monkeypatch):
    """验证省略 leverage 新增敞口时，风控 await 窗口内的外部调杠杆被复核拦截。

    参数：
        tmp_path: Path，pytest 提供的临时目录
        monkeypatch: pytest.MonkeyPatch，用于在 daily_stats_fn 里模拟外部调杠杆

    返回：
        None，断言订单绝不触达网关下单、触发风控锁并 deny
    """
    env = await _make_tools(tmp_path)
    try:
        env.gateway.positions["BTC_USDT"] = _long_position("2")
        engaged = _wire_engage_spy(env)

        async def _flip_leverage():
            """模拟风控 await 窗口内人工/进程外把该合约杠杆改成 20x（超 max_leverage=5）。

            参数：无

            返回：
                DailyStats：全零当日统计（与默认 _zero_daily 同形状）
            """
            env.gateway.positions["BTC_USDT"].leverage = Decimal(20)
            return await _zero_daily()

        placed: list = []
        orig_place = env.gateway.place_order

        def _place_spy(req):
            """记录下单请求后转发原实现。

            参数：
                req: OrderRequest，下单请求

            返回：
                原 place_order 的返回值
            """
            placed.append(req)
            return orig_place(req)

        monkeypatch.setattr(env.deps, "daily_stats_fn", _flip_leverage)
        monkeypatch.setattr(env.gateway, "place_order", _place_spy)
        out = await env.registry.execute(
            "place_order",
            {"contract": "BTC_USDT", "size": 1, "stop_loss_price": 58000},
        )
        assert out.risk_verdict == "deny" and "风控锁" in out.text, out.text
        assert placed == []
        assert len(engaged) == 1
    finally:
        await env.db.close()


async def test_place_order_rollback_verification_mismatch_locks(tmp_path, monkeypatch):
    """验证回滚执行成功但重读核验不一致时触发风控锁。

    参数：
        tmp_path: Path，pytest 提供的临时目录
        monkeypatch: pytest.MonkeyPatch，用于注入回滚被错误执行的调杠杆

    返回：
        None，断言文本含核验不一致提示、回调被触发
    """
    env = await _make_tools(tmp_path)
    try:
        env.gateway.positions["BTC_USDT"] = _long_position("2")
        engaged = _wire_engage_spy(env)
        orig_set = env.gateway.set_leverage
        calls: list = []

        def _wrong_rollback(contract, leverage, margin_mode="isolated"):
            """首次正常设置，回滚时被错误执行为 9x。

            参数：
                contract: str，合约名
                leverage: int，杠杆倍数
                margin_mode: str，保证金模式

            返回：
                原 set_leverage 的返回值（回滚时实际设置为 9x）
            """
            calls.append(leverage)
            if len(calls) == 1:
                return orig_set(contract, leverage, margin_mode)
            return orig_set(contract, 9, margin_mode)

        def _reject(req):
            """模拟交易所明确拒绝下单。

            参数：
                req: OrderRequest，下单请求

            返回：
                None，实际不会返回（总是抛出异常）

            异常：
                GatewayError，模拟交易所明确拒绝（不会重单）
            """
            raise GatewayError("余额不足", label="INSUFFICIENT_BALANCE")

        monkeypatch.setattr(env.gateway, "set_leverage", _wrong_rollback)
        monkeypatch.setattr(env.gateway, "place_order", _reject)
        out = await env.registry.execute(
            "place_order",
            {"contract": "BTC_USDT", "size": 1, "leverage": 3, "stop_loss_price": 58000},
        )
        assert "核验" in out.text and "风控锁" in out.text
        assert len(engaged) == 1
    finally:
        await env.db.close()


async def test_place_order_refuses_when_cross_leverage_unknown(tmp_path):
    """验证全仓实际杠杆未知时拒绝调杠杆下单并触发风控锁（不触达交易所）。

    参数：
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，断言文本含状态未知与风控锁提示、set_leverage 未被调用、回调被触发
    """
    env = await _make_tools(tmp_path)
    try:
        pos = _long_position("0").model_copy(update={"cross_leverage_limit": None})
        env.gateway.positions["BTC_USDT"] = pos  # 全仓但实际杠杆缺失
        spy: list = []
        _spy_set_leverage(env, spy)
        engaged = _wire_engage_spy(env)
        out = await env.registry.execute(
            "place_order",
            {"contract": "BTC_USDT", "size": 1, "leverage": 3, "stop_loss_price": 58000},
        )
        assert "杠杆状态未知" in out.text and "风控锁" in out.text
        assert out.risk_verdict == "deny"  # 硬拒绝按风控拒绝归类，供审计正确统计
        assert spy == []  # 未触达交易所
        assert len(engaged) == 1
    finally:
        await env.db.close()


async def test_place_order_undeclared_leverage_cross_unknown_denied(tmp_path):
    """验证省略 leverage 时，全仓杠杆未知的新增敞口同样被拒绝并触发风控锁。

    参数：
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，断言文本含状态未知与风控锁提示、set_leverage 未被调用、回调被触发
    """
    env = await _make_tools(tmp_path)
    try:
        pos = _long_position("0").model_copy(update={"cross_leverage_limit": None})
        env.gateway.positions["BTC_USDT"] = pos  # 全仓但实际杠杆缺失
        spy: list = []
        _spy_set_leverage(env, spy)
        engaged = _wire_engage_spy(env)
        out = await env.registry.execute(
            "place_order",
            {"contract": "BTC_USDT", "size": 1, "stop_loss_price": 58000},  # 未声明 leverage
        )
        assert "杠杆状态未知" in out.text and "风控锁" in out.text
        assert out.risk_verdict == "deny"
        assert spy == []  # 未触达交易所
        assert len(engaged) == 1
    finally:
        await env.db.close()


async def test_place_order_non_integer_cross_leverage_refused(tmp_path):
    """验证全仓杠杆为小数（lever=4.35 回退路径）时视为不可回滚，新增敞口 fail closed。

    参数：
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，断言文本含状态未知与风控锁提示、set_leverage 未被调用、回调被触发
    """
    env = await _make_tools(tmp_path)
    try:
        pos = _long_position("0", cross_limit="4.35")  # 小数全仓杠杆无法精确回滚
        env.gateway.positions["BTC_USDT"] = pos
        spy: list = []
        _spy_set_leverage(env, spy)
        engaged = _wire_engage_spy(env)
        out = await env.registry.execute(
            "place_order",
            {"contract": "BTC_USDT", "size": 1, "leverage": 3, "stop_loss_price": 58000},
        )
        assert "杠杆状态未知" in out.text and "风控锁" in out.text
        assert out.risk_verdict == "deny"
        assert spy == []  # 未触达交易所
        assert len(engaged) == 1
    finally:
        await env.db.close()


async def test_place_order_zero_size_cross_entry_does_not_lock(tmp_path, monkeypatch):
    """验证 size=0 的历史全仓条目（真实 Gate 会返回）不触发杠杆未知风控锁。

    参数：
        tmp_path: Path，pytest 提供的临时目录
        monkeypatch: pytest.MonkeyPatch，用于注入含零仓条目的持仓列表

    返回：
        None，断言订单成功、未触发风控锁且杠杆正常设置
    """
    env = await _make_tools(tmp_path)
    try:
        dead = _long_position("0").model_copy(
            update={"size": Decimal(0), "cross_leverage_limit": None}
        )
        monkeypatch.setattr(env.gateway, "list_positions", lambda: [dead])
        engaged = _wire_engage_spy(env)
        out = await env.registry.execute(
            "place_order",
            {"contract": "BTC_USDT", "size": 1, "leverage": 3, "stop_loss_price": 58000},
        )
        assert "下单成功" in out.text, out.text
        assert engaged == [] and env.deps.risk_config.kill_switch is False
    finally:
        await env.db.close()


async def test_place_order_close_skips_leverage_and_guard(tmp_path):
    """验证平仓/减仓单不调整杠杆，也不受"杠杆状态未知"守卫拦截。

    参数：
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，断言平仓成功、set_leverage 未被调用、未触发风控锁
    """
    env = await _make_tools(tmp_path)
    try:
        pos = _long_position("0").model_copy(update={"cross_leverage_limit": None})
        env.gateway.positions["BTC_USDT"] = pos  # 全仓但实际杠杆缺失
        spy: list = []
        _spy_set_leverage(env, spy)
        engaged = _wire_engage_spy(env)
        out = await env.registry.execute(
            "place_order", {"contract": "BTC_USDT", "close": True, "leverage": 3}
        )
        assert out.risk_verdict == "allow", out.text
        assert spy == []  # 平仓不调杠杆
        assert engaged == []  # 平仓不被守卫拦截
    finally:
        await env.db.close()


async def test_place_order_unexpected_error_locks_without_rollback(tmp_path, monkeypatch):
    """验证下单抛出非网关异常时不回滚杠杆、触发风控锁并返回禁止重试文案。

    参数：
        tmp_path: Path，pytest 提供的临时目录
        monkeypatch: pytest.MonkeyPatch，用于替换网关下单方法注入未知异常

    返回：
        None，断言文本含禁止重试、杠杆保持修改值、回调被触发（异常不上抛）
    """
    env = await _make_tools(tmp_path)
    try:
        env.gateway.positions["BTC_USDT"] = _long_position("2")
        spy: list = []
        _spy_set_leverage(env, spy)
        engaged = _wire_engage_spy(env)

        def _boom(req):
            """模拟网关内部未知异常（订单是否创建不明）。

            参数：
                req: OrderRequest，下单请求

            返回：
                None，实际不会返回（总是抛出异常）

            异常：
                RuntimeError，模拟非网关类的未预期异常
            """
            raise RuntimeError("网络栈未知错误")

        monkeypatch.setattr(env.gateway, "place_order", _boom)
        out = await env.registry.execute(
            "place_order",
            {"contract": "BTC_USDT", "size": 1, "leverage": 3, "stop_loss_price": 58000},
        )
        assert "下单结果不明" in out.text and "禁止盲目重试" in out.text and "风控锁" in out.text
        assert len(engaged) == 1
        assert spy == [("BTC_USDT", 3, "isolated")]  # 订单状态不明：不回滚
        assert env.gateway.positions["BTC_USDT"].leverage == Decimal(3)
    finally:
        await env.db.close()


async def test_place_order_rollback_restores_mode_switch(tmp_path, monkeypatch):
    """验证全仓 5x 临时切逐仓 3x 下单失败后，回滚同时恢复杠杆与全仓模式。

    参数：
        tmp_path: Path，pytest 提供的临时目录
        monkeypatch: pytest.MonkeyPatch，用于替换网关下单方法注入失败

    返回：
        None，断言回滚调用为 (5, cross)、持仓恢复全仓 5x
    """
    env = await _make_tools(tmp_path)
    try:
        env.gateway.positions["BTC_USDT"] = _long_position("0", "5")  # 全仓 5x
        spy: list = []
        _spy_set_leverage(env, spy)

        def _reject(req):
            """模拟交易所明确拒绝下单。

            参数：
                req: OrderRequest，下单请求

            返回：
                None，实际不会返回（总是抛出异常）

            异常：
                GatewayError，模拟交易所明确拒绝（不会重单）
            """
            raise GatewayError("余额不足", label="INSUFFICIENT_BALANCE")

        monkeypatch.setattr(env.gateway, "place_order", _reject)
        out = await env.registry.execute(
            "place_order",
            {
                "contract": "BTC_USDT",
                "size": 1,
                "leverage": 3,
                "margin_mode": "isolated",
                "stop_loss_price": 58000,
            },
        )
        assert "已回滚至 5（cross）" in out.text
        assert spy == [("BTC_USDT", 3, "isolated"), ("BTC_USDT", 5, "cross")]
        pos = env.gateway.positions["BTC_USDT"]
        assert pos.margin_mode == "cross" and pos.cross_leverage_limit == Decimal(5)
    finally:
        await env.db.close()


async def test_place_order_skips_set_leverage_when_cross_equals_current(tmp_path):
    """验证全仓持仓声明同等杠杆与模式时，不调用交易所 set_leverage。

    参数：
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，断言 set_leverage 探针无调用记录且下单成功
    """
    env = await _make_tools(tmp_path)
    try:
        env.gateway.positions["BTC_USDT"] = _long_position("0", "5")  # 全仓 5x
        spy: list = []
        _spy_set_leverage(env, spy)
        out = await env.registry.execute(
            "place_order",
            {"contract": "BTC_USDT", "size": 1, "leverage": 5, "stop_loss_price": 58000},
        )
        assert out.risk_verdict == "allow", out.text
        assert spy == []  # 目标 (5, cross) 等于现状：不触达交易所
    finally:
        await env.db.close()


def test_resolve_leverage_uses_cross_limit_for_undeclared():
    """验证未声明杠杆时，全仓持仓按 cross_leverage_limit 参与风控判定。

    参数：无

    返回：
        None，断言全仓 10x 解析为 10、全仓缺失回退 1、逐仓 4x 解析为 4
    """
    assert _resolve_leverage("BTC_USDT", None, [_long_position("0", "10")]) == (10, None)
    unknown = _long_position("0").model_copy(update={"cross_leverage_limit": None})
    assert _resolve_leverage("BTC_USDT", None, [unknown]) == (1, None)
    assert _resolve_leverage("BTC_USDT", None, [_long_position("4")]) == (4, None)


async def test_recheck_prev_state_slow_gateway_does_not_block_event_loop(tmp_path):
    """验证杠杆状态重检的网关同步读取经线程卸载，慢响应不阻塞事件循环其他协程。

    参数：
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，断言慢 list_positions 读取期间心跳协程仍持续推进
    """
    env = await _make_tools(tmp_path)
    try:
        env.gateway.positions["BTC_USDT"] = _long_position("2")
        original = env.gateway.list_positions

        def _slow_list_positions():
            """模拟 Gate REST 慢响应：同步阻塞 0.3s 后转发原实现。

            参数：无

            返回：
                list[Position]，原网关 list_positions 返回的持仓快照
            """
            time.sleep(0.3)
            return original()

        env.gateway.list_positions = _slow_list_positions
        ticks = 0
        stop = False

        async def _ticker():
            """每 0.05s 累加一次 tick 的心跳协程，用于探测事件循环是否被阻塞。

            参数：无

            返回：
                None，stop 置位后退出循环
            """
            nonlocal ticks, stop
            while not stop:
                ticks += 1
                await asyncio.sleep(0.05)

        task = asyncio.create_task(_ticker())
        await asyncio.sleep(0)  # 让心跳协程先起跑
        before = ticks
        out = await _recheck_prev_state(env.deps, "BTC_USDT", (2, "isolated"), verify=True)
        stop = True
        await task
        assert out is None  # 杠杆状态一致，无需拒绝
        # 0.3s 慢读取期间心跳持续推进；若读取同步阻塞事件循环，此增量为 0
        assert ticks - before >= 2
    finally:
        await env.db.close()


async def test_place_order_concurrent_leverage_writes_serialized(tmp_path, monkeypatch):
    """验证两个并发 place_order 的杠杆写事务被合约级锁序列化，后进入者重检 fail closed。

    参数：
        tmp_path: Path，pytest 提供的临时目录
        monkeypatch: pytest.MonkeyPatch，用于注入慢 set_leverage 放大交错窗口

    返回：
        None，断言恰好一笔订单提交成功且下单瞬间杠杆等于其声明值、另一笔被风控锁拒绝
        （无锁时两笔都会成功且可能以错误杠杆提交，本测试必红）
    """
    env = await _make_tools(tmp_path)
    try:
        env.gateway.positions["BTC_USDT"] = _long_position("2")
        engaged = _wire_engage_spy(env)
        orig_set = env.gateway.set_leverage

        def _slow_set(contract, leverage, margin_mode="isolated"):
            """慢化 set_leverage 以放大两个写事务的交错窗口。

            参数：
                contract: str，合约名
                leverage: int，杠杆倍数
                margin_mode: str，保证金模式

            返回：
                Position，原 set_leverage 的返回值
            """
            time.sleep(0.2)
            return orig_set(contract, leverage, margin_mode)

        placed: list = []
        placed_leverage: list = []
        orig_place = env.gateway.place_order

        def _place_spy(req):
            """记录下单请求与下单瞬间的持仓杠杆后转发原实现。

            参数：
                req: OrderRequest，下单请求

            返回：
                原 place_order 的返回值
            """
            placed.append(req)
            placed_leverage.append(env.gateway.positions["BTC_USDT"].leverage)
            return orig_place(req)

        monkeypatch.setattr(env.gateway, "set_leverage", _slow_set)
        monkeypatch.setattr(env.gateway, "place_order", _place_spy)
        out_a, out_b = await asyncio.gather(
            env.registry.execute(
                "place_order",
                {"contract": "BTC_USDT", "size": 1, "leverage": 3, "stop_loss_price": 58000},
            ),
            env.registry.execute(
                "place_order",
                {"contract": "BTC_USDT", "size": 1, "leverage": 4, "stop_loss_price": 58000},
            ),
        )
        # 锁序列化：先拿锁者完成整个事务，后拿锁者重检发现快照失效，fail closed
        assert len(placed) == 1
        # 成功单下单瞬间的杠杆等于其声明值（不会出现按 3x 风控却在 4x 状态提交）
        assert placed_leverage[0] in (Decimal(3), Decimal(4))
        texts = [out_a.text, out_b.text]
        assert any("下单成功" in t for t in texts)
        assert any("风控锁" in t for t in texts)
        assert env.deps.risk_config.kill_switch is True
        assert len(engaged) == 1
    finally:
        await env.db.close()


async def test_place_order_confirm_read_detects_external_change_locks(tmp_path, monkeypatch):
    """验证 set_leverage 成功后下单前确认读发现进程外并发修改时，fail closed 且不回滚。

    参数：
        tmp_path: Path，pytest 提供的临时目录
        monkeypatch: pytest.MonkeyPatch，用于模拟进程外并发修改杠杆

    返回：
        None，断言订单未提交、触发风控锁、外部修改后的杠杆未被回滚覆盖
    """
    env = await _make_tools(tmp_path)
    try:
        env.gateway.positions["BTC_USDT"] = _long_position("2")
        engaged = _wire_engage_spy(env)
        orig_set = env.gateway.set_leverage

        def _set_then_external_change(contract, leverage, margin_mode="isolated"):
            """set_leverage 成功后模拟进程外并发把杠杆改成 9x。

            参数：
                contract: str，合约名
                leverage: int，杠杆倍数
                margin_mode: str，保证金模式

            返回：
                Position，原 set_leverage 的返回值
            """
            result = orig_set(contract, leverage, margin_mode)
            env.gateway.positions["BTC_USDT"].leverage = Decimal(9)
            return result

        placed: list = []
        orig_place = env.gateway.place_order

        def _place_spy(req):
            """记录下单请求后转发原实现。

            参数：
                req: OrderRequest，下单请求

            返回：
                原 place_order 的返回值
            """
            placed.append(req)
            return orig_place(req)

        monkeypatch.setattr(env.gateway, "set_leverage", _set_then_external_change)
        monkeypatch.setattr(env.gateway, "place_order", _place_spy)
        out = await env.registry.execute(
            "place_order",
            {"contract": "BTC_USDT", "size": 1, "leverage": 3, "stop_loss_price": 58000},
        )
        assert "风控锁" in out.text and "订单未提交" in out.text
        assert placed == []
        assert len(engaged) == 1
        assert env.deps.risk_config.kill_switch is True
        # 不回滚：外部修改的 9x 状态未被盲写旧快照覆盖
        assert env.gateway.positions["BTC_USDT"].leverage == Decimal(9)
    finally:
        await env.db.close()
