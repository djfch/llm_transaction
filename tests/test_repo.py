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
    assert await repo.count_decisions() == 5


async def test_decisions_page_returns_items_and_total_from_one_query(repo: Repo):
    """越界页仍要带回总数，供前端将页码回退到最后有效页。"""
    for i in range(3):
        await repo.save_decision(round_id=f"page-r{i}", mode="paper")
    items, total = await repo.list_decisions_page(limit=2, offset=4)
    assert items == []
    assert total == 3


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


# ---------- 成交来源（trades.source） ----------


async def test_save_trade_source_roundtrip(repo: Repo):
    """save_trade 透传 source 并落库读回；不传时默认 ''（历史/未知）。"""
    t1 = await repo.save_trade(
        "r1",
        "paper",
        "BTC_USDT",
        Decimal(1),
        Decimal("50000"),
        Decimal("1"),
        Decimal(0),
        source="llm_open",
    )
    t2 = await repo.save_trade(
        "r1", "paper", "BTC_USDT", Decimal(-1), Decimal("51000"), Decimal("1"), Decimal(100)
    )
    assert t1.source == "llm_open"
    assert t2.source == ""
    got = await repo.list_trades()
    assert [t.source for t in got] == ["", "llm_open"]  # 最新在前


async def test_trades_source_migration_adds_column(tmp_path):
    """旧库迁移：已存在的 trades 表（无 source 列）幂等补列，历史行保持 '' 不回填。"""
    import aiosqlite

    path = tmp_path / "old.db"
    conn = await aiosqlite.connect(str(path))
    await conn.execute(
        "CREATE TABLE trades (id INTEGER PRIMARY KEY AUTOINCREMENT, round_id TEXT NOT NULL,"
        " mode TEXT NOT NULL, contract TEXT NOT NULL, size TEXT NOT NULL, price TEXT NOT NULL,"
        " fee TEXT NOT NULL, pnl TEXT NOT NULL, created_at REAL NOT NULL)"
    )
    await conn.execute(
        "INSERT INTO trades(round_id,mode,contract,size,price,fee,pnl,created_at)"
        " VALUES('r0','paper','BTC_USDT','1','50000','1','0',1.0)"
    )
    await conn.commit()
    await conn.close()

    db = Database()
    await db.open(path)  # 迁移应补 source 列；重复 open 幂等
    await db.close()
    db2 = Database()
    await db2.open(path)
    repo = Repo(db2)
    rows = await repo.list_trades()
    assert len(rows) == 1
    assert rows[0].source == ""  # 历史行无法可靠推断来源，保持 '' 不回填
    await db2.close()


# ---------- 成交列表（SQL 层 LIMIT/OFFSET 分页） ----------


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


async def test_list_trades_offset_pagination(repo: Repo):
    """list_trades：offset 分页遍历全量，页间不重复不遗漏。"""
    for i in range(5):
        await repo.save_trade(
            "r1",
            "paper",
            "BTC_USDT",
            Decimal(1),
            Decimal("50000"),
            Decimal("1"),
            Decimal(i),
            created_at=float(i),
        )
    page1 = await repo.list_trades(limit=2, offset=0)
    page2 = await repo.list_trades(limit=2, offset=2)
    page3 = await repo.list_trades(limit=2, offset=4)
    assert [t.pnl for t in page1] == [Decimal(4), Decimal(3)]
    assert [t.pnl for t in page2] == [Decimal(2), Decimal(1)]
    assert [t.pnl for t in page3] == [Decimal(0)]


async def test_count_trades(repo: Repo):
    """count_trades：总数与 list_trades 分页口径一致，支持 contract 过滤。"""
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
    assert await repo.count_trades() == 5
    assert await repo.count_trades(contract="BTC_USDT") == 3
    assert await repo.count_trades(contract="ETH_USDT") == 2


# ---------- notes ----------


async def test_recent_notes(repo: Repo):
    for i in range(5):
        await repo.add_note("r1", f"笔记{i}")
    notes = await repo.recent_notes(3)
    assert [n.content for n in notes] == ["笔记2", "笔记3", "笔记4"]  # 时间正序
    all_notes = await repo.recent_notes(100)
    assert len(all_notes) == 5
    page1 = await repo.list_notes(limit=2, offset=0)
    page2 = await repo.list_notes(limit=2, offset=2)
    assert [note.content for note in page1] == ["笔记4", "笔记3"]
    assert [note.content for note in page2] == ["笔记2", "笔记1"]
    assert await repo.count_notes() == 5


async def test_notes_page_returns_latest_items_and_total(repo: Repo):
    """笔记分页查询使用同一结果快照，越界页也保留总数。"""
    for i in range(3):
        await repo.add_note("r1", f"分页笔记{i}")
    first_items, first_total = await repo.list_notes_page(limit=2, offset=0)
    empty_items, empty_total = await repo.list_notes_page(limit=2, offset=4)
    assert [note.content for note in first_items] == ["分页笔记2", "分页笔记1"]
    assert first_total == empty_total == 3
    assert empty_items == []


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


async def test_latest_audit_round(repo: Repo):
    """latest_audit_round：按 mode 取 started_at 最新一轮；空表 None；模式间隔离。"""
    assert await repo.latest_audit_round("paper") is None  # 空表
    await repo.start_audit_round("r1", "paper", started_at=1000.0)
    await repo.start_audit_round("r2", "paper", started_at=2000.0)
    await repo.start_audit_round("r3", "testnet", started_at=3000.0)  # 其他模式不参与
    latest = await repo.latest_audit_round("paper")
    assert latest is not None
    assert latest.round_id == "r2"  # 同模式内取 started_at 最新
    testnet = await repo.latest_audit_round("testnet")
    assert testnet is not None and testnet.round_id == "r3"
    assert await repo.latest_audit_round("live") is None  # 该模式无记录


async def test_latest_audit_round_tie_breaks_by_insert_order(repo: Repo):
    """started_at 并列时取后插入者（rowid 大者），保证「最新」语义确定。"""
    await repo.start_audit_round("r-first", "paper", started_at=1000.0)
    await repo.start_audit_round("r-second", "paper", started_at=1000.0)
    latest = await repo.latest_audit_round("paper")
    assert latest is not None and latest.round_id == "r-second"


# ---------- decisions/audit_rounds.strategy_md5（策略书原文 md5） ----------


async def test_strategy_md5_write_roundtrip(repo: Repo):
    """save_decision/start_audit_round 透传 strategy_md5 并读回；不传默认 ''。"""
    await repo.save_decision(round_id="r1", mode="paper", strategy_md5="md5-a")
    await repo.start_audit_round("r1", "paper", strategy_md5="md5-a")
    decision = await repo.get_decision_by_round("r1")
    assert decision is not None and decision.strategy_md5 == "md5-a"
    round_row = await repo.get_audit_round("r1")
    assert round_row is not None and round_row.strategy_md5 == "md5-a"
    assert await repo.get_decision_by_round("不存在") is None
    await repo.save_decision(round_id="r2", mode="paper")  # 旧调用方式不受影响
    assert (await repo.get_decision_by_round("r2")).strategy_md5 == ""


async def test_strategy_md5_migration_adds_columns(tmp_path):
    """旧库（decisions/audit_rounds 无 strategy_md5 列）迁移补列；历史行保持 ''，重复 open 幂等。"""
    import aiosqlite

    path = tmp_path / "old.db"
    conn = await aiosqlite.connect(str(path))
    await conn.execute(
        "CREATE TABLE decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, round_id TEXT NOT NULL,"
        " mode TEXT NOT NULL, strategy_version TEXT NOT NULL DEFAULT '',"
        " wake_source TEXT NOT NULL DEFAULT '', context_summary TEXT NOT NULL DEFAULT '',"
        " llm_raw TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL)"
    )
    await conn.execute(
        "CREATE TABLE audit_rounds (round_id TEXT PRIMARY KEY, mode TEXT NOT NULL,"
        " wake_source TEXT NOT NULL DEFAULT '', prompt_md5 TEXT NOT NULL DEFAULT '',"
        " prompt_snapshot TEXT NOT NULL DEFAULT '', context_snapshot TEXT NOT NULL DEFAULT '',"
        " llm_raw TEXT NOT NULL DEFAULT '', started_at REAL NOT NULL, ended_at REAL,"
        " error TEXT NOT NULL DEFAULT '')"
    )
    await conn.execute("INSERT INTO decisions(round_id,mode,created_at) VALUES('r0','paper',1.0)")
    await conn.execute(
        "INSERT INTO audit_rounds(round_id,mode,started_at) VALUES('r0','paper',1.0)"
    )
    await conn.commit()
    await conn.close()

    db = Database()
    await db.open(path)  # 迁移应补 strategy_md5 列；新表随建表出现
    await db.close()
    db2 = Database()
    await db2.open(path)  # 重复 open 幂等
    repo = Repo(db2)
    decision = await repo.get_decision_by_round("r0")
    assert decision is not None and decision.strategy_md5 == ""  # 历史行不回填
    round_row = await repo.get_audit_round("r0")
    assert round_row is not None and round_row.strategy_md5 == ""
    assert await repo.review.list_strategy_versions() == []  # 新表可用
    await db2.close()


# ---------- strategy_versions（策略书版本） ----------


async def test_strategy_version_roundtrip(repo: Repo):
    v1 = await repo.review.save_strategy_version("内容一", "md5-1", "human", "初版")
    v2 = await repo.review.save_strategy_version(
        "内容二", "md5-2", "review_agent", "复盘改写", report_id=7
    )
    assert 0 < v1.id < v2.id
    assert v1.report_id is None
    versions = await repo.review.list_strategy_versions()
    assert [v.md5 for v in versions] == ["md5-2", "md5-1"]  # 按 id 倒序
    got = await repo.review.get_strategy_version(v2.id)
    assert got is not None
    assert got.created_by == "review_agent" and got.report_id == 7
    assert await repo.review.get_strategy_version(999) is None


async def test_attach_report_to_version(repo: Repo):
    """版本先落库、报告后落库：attach_report_to_version 回填 report_id。"""
    v = await repo.review.save_strategy_version("内容", "md5", "review_agent", "复盘改写")
    await repo.review.attach_report_to_version(v.id, 42)
    got = await repo.review.get_strategy_version(v.id)
    assert got is not None and got.report_id == 42


# ---------- review_reports（复盘报告） ----------


async def test_review_report_roundtrip_and_page(repo: Repo):
    await repo.review.save_review_report(1000.0, 2000.0, '{"win_rate":0.5}', "# 报告一", "none")
    r2 = await repo.review.save_review_report(
        2000.0, 3000.0, "{}", "# 报告二", "rewrite", new_version_id=3
    )
    r3 = await repo.review.save_review_report(3000.0, 4000.0, "{}", "", "none", error="LLM 超时")
    items, total = await repo.review.list_review_reports_page(limit=2, offset=0)
    assert [r.id for r in items] == [r3.id, r2.id]  # 最新在前
    assert total == 3
    empty_items, empty_total = await repo.review.list_review_reports_page(limit=2, offset=10)
    assert empty_items == [] and empty_total == 3  # 越界页仍保留总数
    got = await repo.review.get_review_report(r2.id)
    assert got is not None and got.strategy_action == "rewrite" and got.new_version_id == 3
    failed = await repo.review.get_review_report(r3.id)
    assert failed is not None and failed.error == "LLM 超时"
    assert await repo.review.get_review_report(999) is None


async def test_latest_review_period_end(repo: Repo):
    """latest_review_period_end：空库 None；有记录取最大 period_end（调度幂等用）。"""
    assert await repo.review.latest_review_period_end() is None
    await repo.review.save_review_report(1000.0, 2000.0, "{}", "", "none")
    await repo.review.save_review_report(500.0, 1500.0, "{}", "", "none")
    assert await repo.review.latest_review_period_end() == 2000.0


# ---------- 复盘统计取数 ----------


async def _seed_review_trades(repo: Repo) -> None:
    """两策略版本各一轮决策 + 五笔成交（含一笔无决策关联的孤立成交）。"""
    await repo.save_decision(round_id="r-a", mode="paper", strategy_md5="md5-a")
    await repo.save_decision(round_id="r-b", mode="paper", strategy_md5="md5-b")
    await repo.save_decision(round_id="r-c", mode="testnet", strategy_md5="md5-a")
    await repo.save_trade(
        "r-a",
        "paper",
        "BTC_USDT",
        Decimal(1),
        Decimal("50000"),
        Decimal("1"),
        Decimal("10"),
        created_at=1000.0,
    )
    await repo.save_trade(
        "r-a",
        "paper",
        "ETH_USDT",
        Decimal(1),
        Decimal("3000"),
        Decimal("1"),
        Decimal("20"),
        created_at=1500.0,
    )
    await repo.save_trade(
        "r-b",
        "paper",
        "BTC_USDT",
        Decimal(-1),
        Decimal("51000"),
        Decimal("1"),
        Decimal("-30"),
        created_at=2000.0,
    )
    await repo.save_trade(
        "r-c",
        "testnet",
        "BTC_USDT",
        Decimal(1),
        Decimal("50000"),
        Decimal("1"),
        Decimal("40"),
        created_at=2500.0,
    )
    await repo.save_trade(
        "r-orphan",
        "paper",
        "BTC_USDT",
        Decimal(1),
        Decimal("50000"),
        Decimal("1"),
        Decimal("50"),
        created_at=2600.0,
    )


async def test_trades_for_review_filters(repo: Repo):
    """trades_for_review：LEFT JOIN decisions；mode 必填；[start, end)；按 id 正序。

    当前口径：无 strategy_md5 过滤时孤儿成交（无 decisions 行）仍计入基础样本；
    按策略过滤时无 join 匹配的成交不参与（与 INNER JOIN 语义一致）。
    """
    await _seed_review_trades(repo)
    all_paper = await repo.review.trades_for_review(0.0, 3000.0, "paper")
    assert [t.pnl for t in all_paper] == [
        Decimal("10"),
        Decimal("20"),
        Decimal("-30"),
        Decimal("50"),
    ]
    # 孤立成交（r-orphan 无 decisions 行）无过滤时计入基础统计样本
    assert all_paper[-1].round_id == "r-orphan"
    by_md5 = await repo.review.trades_for_review(0.0, 3000.0, "paper", strategy_md5="md5-a")
    assert [t.pnl for t in by_md5] == [Decimal("10"), Decimal("20")]  # 孤儿按策略统计时被排除
    by_contract = await repo.review.trades_for_review(0.0, 3000.0, "paper", contract="ETH_USDT")
    assert [t.pnl for t in by_contract] == [Decimal("20")]
    testnet = await repo.review.trades_for_review(0.0, 3000.0, "testnet", strategy_md5="md5-a")
    assert [t.pnl for t in testnet] == [Decimal("40")]
    ranged = await repo.review.trades_for_review(1000.0, 2000.0, "paper")  # [start, end) 边界
    assert [t.pnl for t in ranged] == [Decimal("10"), Decimal("20")]


async def test_trades_for_review_keeps_orphan_closes_without_strategy_filter(repo: Repo):
    """round_id='' 的孤儿平仓成交在无过滤时计入基础统计，按策略过滤时排除。"""
    await repo.save_trade(
        "",
        "paper",
        "BTC_USDT",
        Decimal(-1),
        Decimal("50000"),
        Decimal("1"),
        Decimal("66"),
        source="user_close",
        created_at=1000.0,
    )
    await repo.save_trade(
        "",
        "paper",
        "BTC_USDT",
        Decimal(-1),
        Decimal("51000"),
        Decimal("1"),
        Decimal("-7"),
        source="liquidation",
        created_at=1100.0,
    )
    hits = await repo.review.trades_for_review(0.0, 2000.0, "paper")
    assert [t.pnl for t in hits] == [Decimal("66"), Decimal("-7")]  # 孤儿成交计入样本
    assert all(t.round_id == "" for t in hits)
    by_md5 = await repo.review.trades_for_review(0.0, 2000.0, "paper", strategy_md5="md5-a")
    assert by_md5 == []  # 按策略统计：无 join 匹配不参与


async def test_decisions_for_review(repo: Repo):
    """decisions_for_review：区间 + strategy_md5 过滤；按 id 倒序；limit 钳 1..100。"""
    now = time.time()
    for i in range(3):
        await repo.save_decision(
            round_id=f"dr{i}", mode="paper", strategy_md5="m1" if i < 2 else "m2"
        )
    items = await repo.review.decisions_for_review(0.0, now + 10)
    assert [d.round_id for d in items] == ["dr2", "dr1", "dr0"]  # 按 id 倒序
    by_md5 = await repo.review.decisions_for_review(0.0, now + 10, strategy_md5="m2")
    assert [d.round_id for d in by_md5] == ["dr2"]
    limited = await repo.review.decisions_for_review(0.0, now + 10, limit=2)
    assert [d.round_id for d in limited] == ["dr2", "dr1"]
    clamped = await repo.review.decisions_for_review(0.0, now + 10, limit=0)  # 钳到 1
    assert len(clamped) == 1
    assert await repo.review.decisions_for_review(now + 100, now + 200) == []  # 区间外


async def test_decisions_for_review_mode_filter(repo: Repo):
    """decisions_for_review 传 mode 时只返回该模式决策；不传时行为不变（全模式）。"""
    now = time.time()
    await repo.save_decision(round_id="dm-p", mode="paper")
    await repo.save_decision(round_id="dm-t", mode="testnet")
    paper = await repo.review.decisions_for_review(0.0, now + 10, mode="paper")
    assert [d.round_id for d in paper] == ["dm-p"]
    assert len(await repo.review.decisions_for_review(0.0, now + 10)) == 2  # 不传 mode 全模式


async def test_list_trades_filtered(repo: Repo):
    """list_trades_filtered：[start, end)；contract/source 可选过滤；按 id 正序；limit 钳 1..200。"""
    for i in range(4):
        await repo.save_trade(
            "r1",
            "paper",
            "BTC_USDT" if i % 2 == 0 else "ETH_USDT",
            Decimal(1),
            Decimal("50000"),
            Decimal("1"),
            Decimal(i),
            source="llm_close" if i < 2 else "user_close",
            created_at=float(1000 + i),
        )
    all_hits = await repo.review.list_trades_filtered(1000.0, 1004.0)
    assert [t.pnl for t in all_hits] == [Decimal(0), Decimal(1), Decimal(2), Decimal(3)]
    by_contract = await repo.review.list_trades_filtered(1000.0, 1004.0, contract="ETH_USDT")
    assert [t.pnl for t in by_contract] == [Decimal(1), Decimal(3)]
    by_source = await repo.review.list_trades_filtered(1000.0, 1004.0, source="llm_close")
    assert [t.pnl for t in by_source] == [Decimal(0), Decimal(1)]
    limited = await repo.review.list_trades_filtered(1000.0, 1004.0, limit=2)
    assert [t.pnl for t in limited] == [Decimal(0), Decimal(1)]
    clamped = await repo.review.list_trades_filtered(1000.0, 1004.0, limit=0)  # 钳到 1
    assert len(clamped) == 1
    assert await repo.review.list_trades_filtered(2000.0, 3000.0) == []  # 区间外


async def test_list_trades_filtered_mode_filter(repo: Repo):
    """list_trades_filtered 传 mode 时只返回该模式成交；不传时行为不变（全模式）。"""
    await repo.save_trade(
        "r1",
        "paper",
        "BTC_USDT",
        Decimal(1),
        Decimal("50000"),
        Decimal("1"),
        Decimal("10"),
        created_at=1000.0,
    )
    await repo.save_trade(
        "r2",
        "testnet",
        "BTC_USDT",
        Decimal(1),
        Decimal("50000"),
        Decimal("1"),
        Decimal("20"),
        created_at=1100.0,
    )
    paper = await repo.review.list_trades_filtered(0.0, 2000.0, mode="paper")
    assert [t.pnl for t in paper] == [Decimal("10")]
    assert len(await repo.review.list_trades_filtered(0.0, 2000.0)) == 2  # 不传 mode 全模式
