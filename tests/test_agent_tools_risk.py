"""工具层测试：amend 过风控、声明杠杆真实生效、落库失败禁止重试、
close 单豁免价格偏离、reduce_only 不计入日下单数、orders.is_close 轻量迁移、
研报方向闸门（高置信反向开仓拦截与各路降级放行）。"""

from __future__ import annotations

import sqlite3
import time
from decimal import Decimal
from types import SimpleNamespace

from src.agent.tool_handlers import ToolDeps
from src.agent.tools import ToolRegistry
from src.config import ResearchConfig, RiskConfig
from src.gateway.base import Contract
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
