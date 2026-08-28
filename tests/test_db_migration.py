"""轻量迁移测试：旧版库文件（缺新列）经 Database.open 后补列，历史行不回填。

风格参照 tests/test_repo.py 的 test_strategy_md5_migration_adds_columns：
先用 aiosqlite 手工建旧表插旧行，再 open 触发完整 SCHEMA + _migrate。
"""

import sqlite3

import aiosqlite
import pytest

from src.memory import Database, Repo

_LEGACY_RESEARCH_REPORTS_DDL = """
CREATE TABLE research_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_type TEXT NOT NULL,
    direction TEXT NOT NULL,
    confidence TEXT NOT NULL,
    created_at REAL NOT NULL
)
"""


async def test_pre_research_database_creates_only_current_research_schema(tmp_path):
    """生产基线没有研报表时，首次启动直接创建当前逐标的结构。

    参数：
        tmp_path: Path，pytest 提供的临时目录
    返回：
        None，执行断言验证目标行为
    """
    path = tmp_path / "pre-research.db"
    conn = await aiosqlite.connect(str(path))
    await conn.execute("CREATE TABLE deployment_baseline (sha TEXT NOT NULL)")
    await conn.execute("INSERT INTO deployment_baseline VALUES ('c7ee59b')")
    await conn.commit()
    await conn.close()

    db = Database()
    await db.open(path)
    cur = await db.conn.execute("PRAGMA table_info(research_reports)")
    columns = {row["name"] for row in await cur.fetchall()}
    assert columns == {
        "id",
        "report_type",
        "schema_version",
        "summary",
        "cross_market_view",
        "global_risks_json",
        "raw_json",
        "error",
        "round_id",
        "created_at",
        "research_prompt_md5",
    }
    await db.close()


async def test_legacy_research_schema_is_rejected_without_mutation(tmp_path):
    """不在支持范围的旧研报库应明确失败，不能静默迁移或部分建表。

    参数：
        tmp_path: Path，pytest 提供的临时目录
    返回：
        None，执行断言验证目标行为
    """
    path = tmp_path / "legacy-research.db"
    conn = await aiosqlite.connect(str(path))
    await conn.execute(_LEGACY_RESEARCH_REPORTS_DDL)
    await conn.execute(
        "INSERT INTO research_reports(report_type,direction,confidence,created_at)"
        " VALUES('manual','偏多','高',1.0)"
    )
    await conn.commit()
    await conn.close()

    db = Database()
    with pytest.raises(RuntimeError, match="研报表结构未知"):
        await db.open(path)
    with pytest.raises(RuntimeError, match="数据库未打开"):
        _ = db.conn

    conn = await aiosqlite.connect(str(path))
    cur = await conn.execute("PRAGMA table_info(research_reports)")
    assert {row[1] for row in await cur.fetchall()} == {
        "id",
        "report_type",
        "direction",
        "confidence",
        "created_at",
    }
    cur = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='research_asset_views'"
    )
    assert await cur.fetchone() is None
    await conn.close()


# 旧版 review_reports 表结构（无 round_id 列），与功能上线前的生产库一致
_OLD_REVIEW_REPORTS_DDL = """
CREATE TABLE review_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    period_start REAL NOT NULL,
    period_end REAL NOT NULL,
    stats_json TEXT NOT NULL DEFAULT '{}',
    report_md TEXT NOT NULL DEFAULT '',
    strategy_action TEXT NOT NULL DEFAULT 'none',
    new_version_id INTEGER,
    error TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
)
"""


async def test_review_reports_round_id_migration(tmp_path):
    """旧库（review_reports 无 round_id 列）迁移补列；老行保持 ''，重复 open 幂等。

    参数：
        tmp_path: Path，pytest 提供的临时目录
    返回：
        None，执行断言验证目标行为
    """
    path = tmp_path / "old.db"
    conn = await aiosqlite.connect(str(path))
    await conn.execute(_OLD_REVIEW_REPORTS_DDL)
    await conn.execute(
        "INSERT INTO review_reports(period_start,period_end,report_md,created_at)"
        " VALUES(1000.0,2000.0,'# 老报告',1.0)"
    )
    await conn.commit()
    await conn.close()

    db = Database()
    await db.open(path)  # open 执行完整 SCHEMA（IF NOT EXISTS 不动旧表）+ _migrate 补列
    cur = await db.conn.execute("PRAGMA table_info(review_reports)")
    assert "round_id" in {row["name"] for row in await cur.fetchall()}  # 列已补上
    repo = Repo(db)
    old = await repo.review.get_review_report(1)
    assert old is not None and old.round_id == ""  # 老报告无审计轮可循，保持 '' 不回填
    saved = await repo.review.save_review_report(
        2000.0, 3000.0, "{}", "# 新报告", "none", round_id="abc"
    )
    got = await repo.review.get_review_report(saved.id)
    assert got is not None and got.round_id == "abc"  # 迁移后新报告正常写入读出
    await db.close()

    db2 = Database()
    await db2.open(path)  # 重复 open 幂等（列已存在，不再 ALTER）
    await db2.close()


# 旧版 causal_links 表结构（无 topic/supersedes_id/await_verification 列），与版本化上线前一致
_OLD_CAUSAL_LINKS_DDL = """
CREATE TABLE causal_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id INTEGER NOT NULL,
    chain_json TEXT NOT NULL,
    confidence REAL NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'pending',
    broken_at INTEGER,
    created_at REAL NOT NULL
)
"""


async def test_causal_links_versioning_migration(tmp_path):
    """旧库（causal_links 缺三列）经补列后重建为三态结构；老链映射 tracking，重复 open 幂等。

    参数：
        tmp_path: Path，pytest 提供的临时目录
    返回：
        None，执行断言验证目标行为
    """
    path = tmp_path / "old-causal.db"
    conn = await aiosqlite.connect(str(path))
    await conn.execute(_OLD_CAUSAL_LINKS_DDL)
    await conn.execute(
        "INSERT INTO causal_links(report_id,chain_json,confidence,created_at)"
        ' VALUES(1,\'[{"node": "老链"}]\',0.6,1.0)'
    )
    await conn.commit()
    await conn.close()

    db = Database()
    await db.open(path)  # 先补 topic/supersedes_id/await_verification 三列，再重建为三态结构
    cur = await db.conn.execute("PRAGMA table_info(causal_links)")
    cols = {row["name"] for row in await cur.fetchall()}
    assert {"topic", "supersedes_id", "status"} <= cols
    assert "broken_at" not in cols and "await_verification" not in cols  # 双字段已合并去除
    cur = await db.conn.execute("PRAGMA index_list(causal_links)")
    index_names = {row["name"] for row in await cur.fetchall()}
    assert "idx_causal_links_supersedes" in index_names  # 迁移末尾补建的索引存在
    repo = Repo(db)
    old = await repo.research.get_causal_link(1)
    assert old is not None
    assert old.topic == ""  # 老链无主题，不回填
    assert old.supersedes_id is None  # 老链无替代关系
    assert old.status == "tracking"  # 老链 pending+待验证 → tracking（进待跟踪池）
    # 迁移后新链正常写入读出（含版本化字段）
    link = await repo.research.save_causal_link(
        report_id=1,
        chain_json='[{"node": "新链"}]',
        confidence=0.7,
        topic="非农",
        status="concluded",
    )
    got = await repo.research.get_causal_link(link.id)
    assert got is not None and got.topic == "非农" and got.status == "concluded"
    await db.close()

    db2 = Database()
    await db2.open(path)  # 重复 open 幂等（已是新结构，各步跳过）
    await db2.close()


# schema_version=2 代际（当前生产基线）的研报表三表结构，迁移输入即此形态
_V2_RESEARCH_REPORTS_DDL = """
CREATE TABLE research_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_type TEXT NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 2,
    summary TEXT NOT NULL DEFAULT '',
    cross_market_view TEXT NOT NULL DEFAULT '',
    global_risks_json TEXT NOT NULL DEFAULT '[]',
    raw_json TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT '',
    round_id TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
)
"""

_V2_RESEARCH_ASSET_VIEWS_DDL = """
CREATE TABLE research_asset_views (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id INTEGER NOT NULL,
    contract TEXT NOT NULL,
    direction TEXT NOT NULL,
    confidence TEXT NOT NULL,
    horizon TEXT NOT NULL DEFAULT '',
    market_regime TEXT NOT NULL DEFAULT '',
    technical_confirmation TEXT NOT NULL DEFAULT '',
    basis_type TEXT NOT NULL DEFAULT '',
    data_status TEXT NOT NULL DEFAULT '',
    evidence_json TEXT NOT NULL DEFAULT '[]',
    risks_json TEXT NOT NULL DEFAULT '[]',
    narrative TEXT NOT NULL DEFAULT '',
    market_context_json TEXT NOT NULL DEFAULT '{}',
    verify_result TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    UNIQUE(report_id, contract)
)
"""

_V2_CAUSAL_LINKS_DDL = """
CREATE TABLE causal_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id INTEGER NOT NULL,
    chain_json TEXT NOT NULL,
    confidence REAL NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'pending',
    broken_at INTEGER,
    topic TEXT NOT NULL DEFAULT '',
    supersedes_id INTEGER,
    await_verification INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL
)
"""


async def _make_v2_db(path) -> aiosqlite.Connection:
    """建一个 v2 代际三表结构的旧库并返回打开的连接（调用方负责插数据与关闭）。

    参数：
        path: Path，目标数据库文件路径

    返回：
        aiosqlite.Connection：已建好 v2 三表结构的打开连接
    """
    conn = await aiosqlite.connect(str(path))
    await conn.execute(_V2_RESEARCH_REPORTS_DDL)
    await conn.execute(_V2_RESEARCH_ASSET_VIEWS_DDL)
    await conn.execute(_V2_CAUSAL_LINKS_DDL)
    return conn


async def test_research_v3_migration_maps_legacy_data(tmp_path):
    """v2 整库迁移：三态映射正确、verify_result 去除、research_prompt_md5 补列且不回填。

    参数：
        tmp_path: Path，pytest 提供的临时目录
    返回：
        None，执行断言验证目标行为
    """
    path = tmp_path / "v2.db"
    conn = await _make_v2_db(path)
    await conn.execute(
        "INSERT INTO research_reports(report_type,schema_version,summary,created_at)"
        " VALUES('manual',2,'旧研报',1.0)"
    )
    await conn.execute(
        "INSERT INTO research_asset_views(report_id,contract,direction,confidence,created_at)"
        " VALUES(1,'BTC_USDT','偏多','高',1.0)"
    )
    for status, await_flag in (("pending", 1), ("pending", 0), ("superseded", 1)):
        await conn.execute(
            "INSERT INTO causal_links(report_id,chain_json,confidence,status,"
            "await_verification,created_at) VALUES(1,'[]',0.5,?,?,1.0)",
            (status, await_flag),
        )
    await conn.commit()
    await conn.close()

    db = Database()
    await db.open(path)
    repo = Repo(db)
    # 三态映射：pending+待验证→tracking、pending+结论→concluded、superseded 不变
    migrated = [await repo.research.get_causal_link(i) for i in (1, 2, 3)]
    assert [link.status for link in migrated if link] == ["tracking", "concluded", "superseded"]
    cur = await db.conn.execute("PRAGMA table_info(research_asset_views)")
    assert "verify_result" not in {row["name"] for row in await cur.fetchall()}
    cur = await db.conn.execute("PRAGMA table_info(research_reports)")
    assert "research_prompt_md5" in {row["name"] for row in await cur.fetchall()}
    cur = await db.conn.execute("SELECT research_prompt_md5 FROM research_reports WHERE id=1")
    row = await cur.fetchone()
    assert row is not None and row["research_prompt_md5"] == ""  # 历史研报无 md5 可循，不回填
    views = await repo.research.list_asset_views_by_report(1)  # 数据行保留
    assert len(views) == 1 and views[0].contract == "BTC_USDT" and views[0].direction == "偏多"
    cur = await db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
        " AND name IN ('research_reviews','research_prompt_versions')"
    )
    assert {row["name"] for row in await cur.fetchall()} == {
        "research_reviews",
        "research_prompt_versions",
    }  # 旧库同样建出两张新表
    await db.close()

    db2 = Database()
    await db2.open(path)  # 重复 open 幂等（重建后的新结构不再触发迁移）
    await db2.close()


async def test_research_v3_migration_rejects_unknown_causal_status(tmp_path):
    """causal_links 存在 verified/failed 等未知状态时迁移拒绝启动（提示备份，不静默丢弃）。

    参数：
        tmp_path: Path，pytest 提供的临时目录
    返回：
        None，执行断言验证目标行为
    """
    path = tmp_path / "v2-bad-status.db"
    conn = await _make_v2_db(path)
    await conn.execute(
        "INSERT INTO causal_links(report_id,chain_json,confidence,status,created_at)"
        " VALUES(1,'[]',0.5,'verified',1.0)"
    )
    await conn.commit()
    await conn.close()

    db = Database()
    with pytest.raises(RuntimeError, match="未知状态"):
        await db.open(path)


async def test_research_v3_migration_rejects_non_null_broken_at(tmp_path):
    """causal_links.broken_at 存在非空数据时迁移拒绝启动（当前版本无该列写入路径）。

    参数：
        tmp_path: Path，pytest 提供的临时目录
    返回：
        None，执行断言验证目标行为
    """
    path = tmp_path / "v2-broken.db"
    conn = await _make_v2_db(path)
    await conn.execute(
        "INSERT INTO causal_links(report_id,chain_json,confidence,status,broken_at,created_at)"
        " VALUES(1,'[]',0.5,'pending',1,1.0)"
    )
    await conn.commit()
    await conn.close()

    db = Database()
    with pytest.raises(RuntimeError, match="broken_at"):
        await db.open(path)


async def test_research_v3_migration_rejects_non_empty_verify_result(tmp_path):
    """research_asset_views.verify_result 存在非空数据时迁移拒绝启动（死字段非空即未知来源）。

    参数：
        tmp_path: Path，pytest 提供的临时目录
    返回：
        None，执行断言验证目标行为
    """
    path = tmp_path / "v2-verify.db"
    conn = await _make_v2_db(path)
    await conn.execute(
        "INSERT INTO research_reports(report_type,schema_version,created_at) VALUES('manual',2,1.0)"
    )
    await conn.execute(
        "INSERT INTO research_asset_views(report_id,contract,direction,confidence,"
        "verify_result,created_at) VALUES(1,'BTC_USDT','偏多','高','命中',1.0)"
    )
    await conn.commit()
    await conn.close()

    db = Database()
    with pytest.raises(RuntimeError, match="verify_result"):
        await db.open(path)


async def test_research_v3_rebuild_failure_rolls_back_and_retries(tmp_path, monkeypatch):
    """重建中途失败时 SAVEPOINT 回滚：旧表数据完整，故障消除后重开库迁移成功。

    参数：
        tmp_path: Path，pytest 提供的临时目录
        monkeypatch: pytest.MonkeyPatch，注入 COPY 语句故障的夹具

    返回：
        None，执行断言验证目标行为
    """
    from src.memory import migrate_v3

    path = tmp_path / "v2-fail.db"
    conn = await _make_v2_db(path)
    await conn.execute(
        "INSERT INTO research_asset_views(report_id,contract,direction,confidence,created_at)"
        " VALUES(1,'BTC_USDT','偏多','高',1.0)"
    )
    await conn.commit()
    await conn.close()

    # 故障注入：COPY 引用不存在的表 → DDL 已完成、COPY 失败的中途现场
    monkeypatch.setattr(
        migrate_v3,
        "_ASSET_VIEWS_V3_COPY",
        "INSERT INTO research_asset_views_v3(id,report_id,contract)"
        " SELECT id,report_id,contract FROM no_such_table",
    )
    db = Database()
    with pytest.raises(Exception, match="no_such_table"):
        await db.open(path)
    await db.close()  # 清理半开连接（重复调用安全）

    # SAVEPOINT 回滚：旧表未被 DROP，数据行与旧结构（含 verify_result 列）完整保留
    conn2 = await aiosqlite.connect(str(path))
    cur = await conn2.execute("SELECT COUNT(*) FROM research_asset_views WHERE contract='BTC_USDT'")
    row = await cur.fetchone()
    assert row is not None and row[0] == 1
    cur = await conn2.execute("PRAGMA table_info(research_asset_views)")
    assert "verify_result" in {r[1] for r in await cur.fetchall()}  # 裸连接行为 tuple
    await conn2.close()

    monkeypatch.undo()  # 故障消除后迁移可重试
    db2 = Database()
    await db2.open(path)
    views = await Repo(db2).research.list_asset_views_by_report(1)
    assert len(views) == 1 and views[0].contract == "BTC_USDT" and views[0].direction == "偏多"
    await db2.close()


async def test_research_schema_validation_rejects_unknown_schema_version(tmp_path):
    """存在 schema_version 非 2/3 的研报数据时拒绝启动（无论结构是哪一代）。

    参数：
        tmp_path: Path，pytest 提供的临时目录
    返回：
        None，执行断言验证目标行为
    """
    path = tmp_path / "v2-bad-version.db"
    conn = await _make_v2_db(path)
    await conn.execute(
        "INSERT INTO research_reports(report_type,schema_version,created_at) VALUES('manual',4,1.0)"
    )
    await conn.commit()
    await conn.close()

    db = Database()
    with pytest.raises(RuntimeError, match="非 schema_version"):
        await db.open(path)


async def test_fresh_database_creates_research_review_tables(tmp_path):
    """新库直接建出 research_reviews / research_prompt_versions 两张新表。

    参数：
        tmp_path: Path，pytest 提供的临时目录
    返回：
        None，执行断言验证目标行为
    """
    path = tmp_path / "fresh.db"
    db = Database()
    await db.open(path)
    cur = await db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
        " AND name IN ('research_reviews','research_prompt_versions')"
    )
    assert {row["name"] for row in await cur.fetchall()} == {
        "research_reviews",
        "research_prompt_versions",
    }
    await db.close()


async def test_research_reviews_unique_constraint(tmp_path):
    """research_reviews：同一复盘报告内 (report_id, contract) 唯一；不同复盘报告可复评同一结论。

    参数：
        tmp_path: Path，pytest 提供的临时目录
    返回：
        None，执行断言验证目标行为
    """
    path = tmp_path / "unique.db"
    db = Database()
    await db.open(path)
    await db.conn.execute(
        "INSERT INTO research_reviews(review_report_id,report_id,contract,created_at)"
        " VALUES(1,7,'BTC_USDT',1.0)"
    )
    await db.conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        await db.conn.execute(
            "INSERT INTO research_reviews(review_report_id,report_id,contract,created_at)"
            " VALUES(1,7,'BTC_USDT',2.0)"
        )
    await db.conn.rollback()  # 清理失败语句的事务状态
    await db.conn.execute(
        "INSERT INTO research_reviews(review_report_id,report_id,contract,created_at)"
        " VALUES(2,7,'BTC_USDT',2.0)"
    )  # 另一份复盘报告可复评同一研报同一合约
    await db.conn.commit()
    await db.close()
