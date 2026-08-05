"""工具层测试：amend 过风控、声明杠杆真实生效、落库失败禁止重试、
close 单豁免价格偏离、reduce_only 不计入日下单数、orders.is_close 轻量迁移。"""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from types import SimpleNamespace

from src.agent.tool_handlers import ToolDeps
from src.agent.tools import ToolRegistry
from src.config import RiskConfig
from src.gateway.base import Contract
from src.gateway.mock import MockGateway
from src.market.candles import CandleCache, ManualPriceSource
from src.market.triggers import TriggerManager
from src.memory import Database, Repo
from src.risk.engine import RiskEngine
from src.risk.models import DailyStats


def _contract(name: str, quanto: str, mark: str) -> Contract:
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
    return DailyStats(realized_pnl=Decimal(0), orders_today=0)


async def _make_tools(tmp_path, *, extra_contracts: tuple = ()) -> SimpleNamespace:
    """组装工具注册表（MockGateway + tmp_path SQLite）。"""
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
        mode="paper",
        round_id="r-test",
    )
    return SimpleNamespace(
        db=db, repo=repo, gateway=gateway, deps=deps, registry=ToolRegistry(deps)
    )


async def _open_limit_order(env: SimpleNamespace, size: int = 1, price: int = 59000) -> str:
    """下一笔限价挂单（保持 open），返回该挂单号。"""
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
    env = await _make_tools(tmp_path, extra_contracts=("DOGE_USDT",))
    try:
        out = await env.registry.execute(
            "amend_order", {"contract": "DOGE_USDT", "order_id": "x", "size": 1}
        )
        assert out.risk_verdict == "deny" and "白名单" in out.text
    finally:
        await env.db.close()


async def test_amend_order_allowed_and_persisted(tmp_path):
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
    """反向改单数量超过持仓 = 翻仓（新敞口），不得豁免 kill_switch（回归 #翻仓豁免洞）。"""
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
    env = await _make_tools(tmp_path)
    try:
        spy = []
        orig = env.gateway.set_leverage

        def _spy(contract, leverage, margin_mode="isolated"):
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
    env = await _make_tools(tmp_path)
    try:

        async def _boom(**kwargs):
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
