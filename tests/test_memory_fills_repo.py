"""ExchangeFillsRepo 单元测试：交易所成交的幂等落库、水线、归属补正与 pnl 回填。"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from src.memory.db import Database
from src.memory.fills_repo import ExchangeFillsRepo
from src.memory.repo import Repo


async def _make_env(tmp_path) -> SimpleNamespace:
    """构造指向临时数据库的测试环境（数据库、Repo 与交易所成交仓储）。

    参数：
        tmp_path: Path，pytest 临时目录夹具，SQLite 数据库文件落在其中

    返回：
        SimpleNamespace：含 db（已打开的 Database）、repo（成交读取仓储）、
        fills（交易所成交仓储 ExchangeFillsRepo）
    """
    db = Database()
    await db.open(tmp_path / "fills.db")
    return SimpleNamespace(db=db, repo=Repo(db), fills=ExchangeFillsRepo(db))


async def _save(env: SimpleNamespace, tid: str = "t1", order_id: str = "o1", **kw) -> int | None:
    """向交易所成交表写入一条默认测试记录，字段可用关键字参数覆盖。

    参数：
        env: SimpleNamespace，_make_env 构造的测试环境，使用其中的 fills 仓储
        tid: str，交易所成交 id（exchange_trade_id），默认 "t1"
        order_id: str，交易所订单 id（exchange_order_id），默认 "o1"
        **kw: 任意关键字参数，覆盖默认成交字段（如 created_at、price 等）

    返回：
        int | None：首次插入返回行 id，重复 exchange_trade_id 返回 None
    """
    kwargs = dict(
        exchange_trade_id=tid,
        exchange_order_id=order_id,
        round_id="r1",
        mode="testnet",
        contract="BTC_USDT",
        size=Decimal("1"),
        price=Decimal("60000"),
        fee=Decimal("0.01"),
        pnl=Decimal(0),
        source="",
        created_at=1000.0,
    )
    kwargs.update(kw)
    return await env.fills.save_exchange_trade(**kwargs)


async def test_save_exchange_trade_idempotent(tmp_path):
    """同一 exchange_trade_id 首次插入返回行 id，重复写入返回 None 且行数不变。

    参数：
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，通过断言验证上述行为，无返回值
    """
    env = await _make_env(tmp_path)
    try:
        row_id = await _save(env)
        assert row_id is not None
        assert await _save(env) is None  # ON CONFLICT DO NOTHING
        trades = await env.repo.trades_between(0.0, 2000.0)
        assert len(trades) == 1
    finally:
        await env.db.close()


async def test_latest_exchange_ts_empty_then_max(tmp_path):
    """无交易所成交水线为 None；有记录取最大 created_at（不含 paper 行），按 mode 隔离。

    参数：
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，通过断言验证上述行为，无返回值
    """
    env = await _make_env(tmp_path)
    try:
        assert await env.fills.latest_exchange_ts("testnet") is None
        await env.repo.save_trade(  # paper 行（无 exchange_trade_id）不参与水线
            round_id="r0",
            mode="paper",
            contract="BTC_USDT",
            size=Decimal(1),
            price=Decimal("60000"),
            fee=Decimal(0),
            pnl=Decimal(0),
            source="llm_open",
        )
        assert await env.fills.latest_exchange_ts("testnet") is None
        await _save(env, "t1", created_at=1000.0)
        await _save(env, "t2", "o2", created_at=1200.0)
        assert await env.fills.latest_exchange_ts("testnet") == 1200.0
        assert await env.fills.latest_exchange_ts("live") is None  # 异模式互不影响
    finally:
        await env.db.close()


async def test_find_and_update_attribution(tmp_path):
    """按交易所订单 id 查到成交行后可补正来源与归属轮；查询按 mode 隔离。

    参数：
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，通过断言验证上述行为，无返回值
    """
    env = await _make_env(tmp_path)
    try:
        row_id = await _save(env, "t1", "o-auto")
        rows = await env.fills.find_by_exchange_order_id("o-auto", "testnet")
        assert rows == [(row_id, "", "BTC_USDT", 1000.0)]
        assert await env.fills.find_by_exchange_order_id("o-auto", "live") == []  # 异模式不可见
        await env.fills.update_attribution(row_id, source="tpsl_close", round_id="")
        rows = await env.fills.find_by_exchange_order_id("o-auto", "testnet")
        assert rows[0][1] == "tpsl_close"
        assert await env.fills.find_by_exchange_order_id("o-none", "testnet") == []
    finally:
        await env.db.close()


async def test_update_pnl(tmp_path):
    """校验 update_pnl 按行 id 回填成交盈亏，读回后为更新后的值。

    参数：
        tmp_path: Path，pytest 临时目录夹具，数据库文件落在其中

    返回：
        None，断言读回的成交记录 pnl 等于回填的 Decimal("-3.5")
    """
    env = await _make_env(tmp_path)
    try:
        row_id = await _save(env)
        await env.fills.update_pnl(row_id, Decimal("-3.5"))
        [trade] = await env.repo.trades_between(0.0, 2000.0)
        assert trade.pnl == Decimal("-3.5")
    finally:
        await env.db.close()


async def test_order_attribution(tmp_path):
    """本地订单归属：返回 (round_id, trade_source, is_close)；无订单返回 None；

    参数：
        tmp_path: Path，pytest 提供的临时目录

    返回：
        None，通过断言验证上述行为，无返回值
    """
    env = await _make_env(tmp_path)
    try:
        await env.repo.save_order(
            order_id="o1",
            round_id="r-9",
            mode="testnet",
            contract="BTC_USDT",
            side_size=Decimal(-1),
            price=None,
            is_close=True,
            trade_source="user_close",
        )
        assert await env.fills.order_attribution("o1", "testnet") == ("r-9", "user_close", True)
        assert await env.fills.order_attribution("o1", "live") is None  # 异模式撞号不误判
        assert await env.fills.order_attribution("o-none", "testnet") is None
    finally:
        await env.db.close()
