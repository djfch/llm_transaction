"""轻量迁移测试：旧版库文件（缺新列）经 Database.open 后补列，历史行不回填。

风格参照 tests/test_repo.py 的 test_strategy_md5_migration_adds_columns：
先用 aiosqlite 手工建旧表插旧行，再 open 触发完整 SCHEMA + _migrate。
"""

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
    with pytest.raises(RuntimeError, match="旧版研报表"):
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
    """旧库（causal_links 缺三列）迁移补列；老行保持 主题''/无替代/待验证，重复 open 幂等。

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
    await db.open(path)  # 完整 SCHEMA（IF NOT EXISTS 不动旧表）+ _migrate 补列 + 补索引
    cur = await db.conn.execute("PRAGMA table_info(causal_links)")
    cols = {row["name"] for row in await cur.fetchall()}
    assert {"topic", "supersedes_id", "await_verification"} <= cols  # 三列已补上
    cur = await db.conn.execute("PRAGMA index_list(causal_links)")
    index_names = {row["name"] for row in await cur.fetchall()}
    assert "idx_causal_links_supersedes" in index_names  # 迁移末尾补建的索引存在
    repo = Repo(db)
    old = await repo.research.get_causal_link(1)
    assert old is not None
    assert old.topic == ""  # 老链无主题，不回填
    assert old.supersedes_id is None  # 老链无替代关系
    assert old.await_verification is True  # 老链按待验证处理（进未闭合监控池）
    assert old.status == "pending"
    # 迁移后新链正常写入读出（含版本化字段）
    link = await repo.research.save_causal_link(
        report_id=1,
        chain_json='[{"node": "新链"}]',
        confidence=0.7,
        topic="非农",
        await_verification=False,
    )
    got = await repo.research.get_causal_link(link.id)
    assert got is not None and got.topic == "非农" and got.await_verification is False
    await db.close()

    db2 = Database()
    await db2.open(path)  # 重复 open 幂等（列与索引已存在）
    await db2.close()
