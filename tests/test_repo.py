"""src/memory 持久化层测试：tmp_path 上的真实 SQLite，覆盖各表写入/查询/分页。"""

import time
from decimal import Decimal

import pytest

from src.memory import Database, Repo
from src.risk.models import DailyStats


@pytest.fixture
async def db(tmp_path):
    database = Database()
    await database.open(tmp_path / "test.db")
    yield database
    await database.close()


@pytest.fixture
async def repo(db: Database) -> Repo:
    return Repo(db)


# ---------- 连接与建表 ----------


async def test_wal_mode_enabled(db: Database):
    cur = await db.conn.execute("PRAGMA journal_mode")
    row = await cur.fetchone()
    assert row[0] == "wal"


async def test_reopen_is_idempotent(tmp_path):
    path = tmp_path / "reopen.db"
    db1 = Database()
    await db1.open(path)
    await db1.close()
    db2 = Database()  # 重复 open 同一文件不应报错（IF NOT EXISTS）
    await db2.open(path)
    await db2.close()


async def test_conn_before_open_raises():
    with pytest.raises(RuntimeError, match="未打开"):
        _ = Database().conn


# ---------- decisions ----------


async def test_save_and_list_decisions(repo: Repo):
    saved = await repo.save_decision(
        round_id="r1",
        mode="paper",
        strategy_version="v1",
        wake_source="timer",
        context_summary="概要",
        llm_raw='{"a":1}',
    )
    assert saved.id > 0
    items = await repo.list_decisions()
    assert len(items) == 1
    assert items[0].round_id == "r1"
    assert items[0].llm_raw == '{"a":1}'


async def test_decisions_pagination(repo: Repo):
    for i in range(5):
        await repo.save_decision(round_id=f"r{i}", mode="paper")
    page1 = await repo.list_decisions(limit=2, offset=0)
    page2 = await repo.list_decisions(limit=2, offset=2)
    page3 = await repo.list_decisions(limit=2, offset=4)
    assert [d.round_id for d in page1] == ["r4", "r3"]  # 最新在前
    assert [d.round_id for d in page2] == ["r2", "r1"]
    assert [d.round_id for d in page3] == ["r0"]


# ---------- orders ----------


async def test_order_write_update_query(repo: Repo):
    await repo.save_order(
        order_id="12345",
        round_id="r1",
        mode="paper",
        contract="BTC_USDT",
        side_size=Decimal(2),
        price=Decimal("50000.5"),
        tif="gtc",
        text="t-abc",
    )
    await repo.save_order(
        order_id="12346",
        round_id="r1",
        mode="paper",
        contract="ETH_USDT",
        side_size=Decimal(-1),
        tif="ioc",
        status="finished",
        finish_as="filled",
    )
    await repo.save_order(  # 另一轮，不应被查出
        order_id="999",
        round_id="r2",
        mode="paper",
        contract="BTC_USDT",
        side_size=Decimal(1),
    )
    orders = await repo.list_orders("r1")
    assert [o.id for o in orders] == ["12345", "12346"]
    assert orders[0].price == Decimal("50000.5")
    assert orders[1].price is None  # 市价单（空串还原为 None）
    assert orders[1].side_size == Decimal(-1)

    await repo.update_order_status("12345", "finished", "cancelled")
    updated = await repo.list_orders("r1")
    assert updated[0].status == "finished"
    assert updated[0].finish_as == "cancelled"


# ---------- trades ----------


async def test_trades_between_range(repo: Repo):
    await repo.save_trade(
        "r1",
        "paper",
        "BTC_USDT",
        Decimal(1),
        Decimal("50000"),
        Decimal("0.5"),
        Decimal("0"),
        created_at=1000.0,
    )
    await repo.save_trade(
        "r1",
        "paper",
        "BTC_USDT",
        Decimal(-1),
        Decimal("51000"),
        Decimal("0.5"),
        Decimal("100"),
        created_at=2000.0,
    )
    await repo.save_trade(
        "r2",
        "paper",
        "ETH_USDT",
        Decimal(1),
        Decimal("3000"),
        Decimal("0.1"),
        Decimal("0"),
        created_at=3000.0,
    )
    hits = await repo.trades_between(1500.0, 2500.0)
    assert len(hits) == 1
    assert hits[0].pnl == Decimal("100")
    assert hits[0].price == Decimal("51000")


async def test_trade_decimal_precision(repo: Repo):
    tiny = Decimal("0.00000001")  # TEXT 存储不丢精度
    await repo.save_trade("r1", "paper", "BTC_USDT", tiny, tiny, tiny, tiny, created_at=1.0)
    got = (await repo.trades_between(0.0, 2.0))[0]
    assert got.size == tiny
    assert got.fee == tiny


async def test_trades_between_mode_filter(repo: Repo):
    """trades_between 传 mode 时只返回该模式成交；不传时行为不变（全模式）。"""
    await repo.save_trade(
        "r1",
        "paper",
        "BTC_USDT",
        Decimal(1),
        Decimal("50000"),
        Decimal("1"),
        Decimal("100"),
        created_at=1000.0,
    )
    await repo.save_trade(
        "r2",
        "testnet",
        "BTC_USDT",
        Decimal(1),
        Decimal("50000"),
        Decimal("1"),
        Decimal("200"),
        created_at=1500.0,
    )
    paper = await repo.trades_between(0.0, 2000.0, mode="paper")
    assert [t.pnl for t in paper] == [Decimal("100")]
    assert len(await repo.trades_between(0.0, 2000.0)) == 2


# ---------- 日统计（daily_stats） ----------


async def test_daily_stats_filters_mode_and_close_orders(repo: Repo):
    """日统计按 mode 过滤；orders_today 只计开仓单（平仓单落库 side_size='0'）。"""
    now = time.localtime()
    day_start = time.mktime((now.tm_year, now.tm_mon, now.tm_mday, 0, 0, 0, 0, 0, -1))
    await repo.save_order("o1", "r1", "paper", "BTC_USDT", Decimal(2))  # 开多
    await repo.save_order("o2", "r1", "paper", "ETH_USDT", Decimal(-1))  # 开空
    await repo.save_order("o3", "r1", "paper", "BTC_USDT", Decimal(0))  # 平仓单不计
    await repo.save_order("o4", "r1", "testnet", "BTC_USDT", Decimal(1))  # 其他模式不计
    await repo.save_trade(
        "r1", "paper", "BTC_USDT", Decimal(1), Decimal("50000"), Decimal("2"), Decimal("100")
    )
    await repo.save_trade(
        "r1", "paper", "ETH_USDT", Decimal(-1), Decimal("3000"), Decimal("1"), Decimal("-30")
    )
    await repo.save_trade(
        "r1", "testnet", "BTC_USDT", Decimal(1), Decimal("50000"), Decimal("1"), Decimal("999")
    )  # 其他模式不计
    await repo.save_trade(
        "r0",
        "paper",
        "BTC_USDT",
        Decimal(1),
        Decimal("50000"),
        Decimal("1"),
        Decimal("777"),
        created_at=day_start - 10,
    )  # 昨日不计
    stats = await repo.daily_stats("paper", day_start)
    assert isinstance(stats, DailyStats)
    assert stats.orders_today == 2
    assert stats.realized_pnl == Decimal("70")


async def test_daily_stats_empty_day(repo: Repo):
    """当日无成交无订单时返回零值统计。"""
    stats = await repo.daily_stats("paper", day_start_ts=time.time() + 3600)
    assert stats.orders_today == 0
    assert stats.realized_pnl == Decimal(0)


# ---------- 成交列表（SQL 层 LIMIT） ----------


async def test_list_trades_limit_and_contract(repo: Repo):
    """list_trades：SQL 层 LIMIT、最新在前、合约可选过滤。"""
    for i in range(5):
        await repo.save_trade(
            "r1",
            "paper",
            "BTC_USDT" if i % 2 == 0 else "ETH_USDT",
            Decimal(1),
            Decimal("50000"),
            Decimal("1"),
            Decimal(i),
            created_at=float(i),
        )
    recent = await repo.list_trades(limit=3)
    assert [t.pnl for t in recent] == [Decimal(4), Decimal(3), Decimal(2)]  # 最新在前
    btc = await repo.list_trades(limit=200, contract="BTC_USDT")
    assert [t.pnl for t in btc] == [Decimal(4), Decimal(2), Decimal(0)]


# ---------- notes ----------


async def test_recent_notes(repo: Repo):
    for i in range(5):
        await repo.add_note("r1", f"笔记{i}")
    notes = await repo.recent_notes(3)
    assert [n.content for n in notes] == ["笔记2", "笔记3", "笔记4"]  # 时间正序
    all_notes = await repo.recent_notes(100)
    assert len(all_notes) == 5


# ---------- alerts ----------


async def test_alerts_add_deactivate_list(repo: Repo):
    a1 = await repo.add_alert("r1", "BTC_USDT", "above", Decimal("60000"))
    await repo.add_alert("r1", "ETH_USDT", "below", Decimal("2500"))
    assert len(await repo.list_alerts()) == 2
    await repo.deactivate_alert(a1.id)
    active = await repo.list_alerts()
    assert len(active) == 1
    assert active[0].contract == "ETH_USDT"
    assert active[0].active is True
    assert len(await repo.list_alerts(active_only=False)) == 2


# ---------- wakeup ----------


async def test_record_wakeup(repo: Repo, db: Database):
    await repo.record_wakeup(scheduled_at=12345.0, source="timer")
    cur = await db.conn.execute("SELECT scheduled_at, source FROM wakeup")
    row = await cur.fetchone()
    assert row["scheduled_at"] == 12345.0
    assert row["source"] == "timer"


# ---------- audit ----------


async def test_audit_round_lifecycle(repo: Repo):
    await repo.start_audit_round(
        round_id="r1",
        mode="paper",
        wake_source="price_alert",
        prompt_md5="abc123",
        prompt_snapshot="完整提示词",
        context_snapshot="上下文全文",
    )
    unfinished = await repo.get_audit_round("r1")
    assert unfinished is not None
    assert unfinished.ended_at is None
    assert unfinished.prompt_md5 == "abc123"

    await repo.finish_audit_round("r1", llm_raw="LLM原始输出", error="")
    done = await repo.get_audit_round("r1")
    assert done is not None
    assert done.llm_raw == "LLM原始输出"
    assert done.ended_at is not None
    assert done.error == ""
    assert await repo.get_audit_round("不存在") is None


async def test_audit_tool_calls_by_round(repo: Repo):
    await repo.start_audit_round(round_id="r1", mode="paper")
    await repo.save_audit_tool_call(
        round_id="r1",
        seq=2,
        tool="place_order",
        args_json='{"size":1}',
        risk_verdict="allow",
        result_json='{"id":"1"}',
        duration_ms=120,
    )
    await repo.save_audit_tool_call(
        round_id="r1",
        seq=1,
        tool="get_account",
        risk_verdict="allow",
        duration_ms=30,
    )
    await repo.save_audit_tool_call(round_id="r2", seq=1, tool="add_note")
    calls = await repo.list_audit_tool_calls("r1")
    assert [c.seq for c in calls] == [1, 2]  # 按 seq 排序
    assert calls[1].tool == "place_order"
    assert calls[1].duration_ms == 120
    assert calls[1].risk_verdict == "allow"


async def test_list_audit_rounds_batch(repo: Repo):
    """list_audit_rounds：一次 IN 查询批量取（避免列表端点 N+1），缺失的 id 不出现。"""
    await repo.start_audit_round("r1", "paper", prompt_md5="m1")
    await repo.start_audit_round("r2", "testnet", prompt_md5="m2")
    got = await repo.list_audit_rounds(["r2", "r1", "不存在"])
    assert set(got) == {"r1", "r2"}
    assert got["r2"].prompt_md5 == "m2"
    assert got["r1"].mode == "paper"
    assert await repo.list_audit_rounds([]) == {}
