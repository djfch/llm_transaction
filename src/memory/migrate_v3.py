"""研报表 v2→v3 结构迁移（issue #113）：由 db._migrate 调用，幂等。

三步：
1. research_reports 补 research_prompt_md5 列（ALTER，历史行默认 '' 不回填）；
2. research_asset_views 重建去除 verify_result 死字段（历史无写入路径）；
3. causal_links 重建：status+await_verification 双字段合并为 tracking/concluded/
   superseded 三态，去除 broken_at 列。

两步重建前均做异常值检查：verify_result 非空、causal_links 出现 verified/failed 等
未知状态或 broken_at 非空，均属当前版本无写入路径的未知数据——拒绝启动并提示备份，
不静默丢弃。

两段重建的 DDL→COPY→DROP→RENAME→INDEX 序列均以 SAVEPOINT 包裹：中途失败
（如表锁、磁盘满）回滚到保存点后重抛，旧表保持完整，下次启动可安全重试。
"""

from __future__ import annotations

import aiosqlite

_ASSET_VIEWS_V3_DDL = """
CREATE TABLE research_asset_views_v3 (
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
    created_at REAL NOT NULL,
    UNIQUE(report_id, contract)
)
"""

_ASSET_VIEWS_V3_COPY = """
INSERT INTO research_asset_views_v3(
    id,report_id,contract,direction,confidence,horizon,market_regime,
    technical_confirmation,basis_type,data_status,evidence_json,risks_json,
    narrative,market_context_json,created_at)
SELECT id,report_id,contract,direction,confidence,horizon,market_regime,
    technical_confirmation,basis_type,data_status,evidence_json,risks_json,
    narrative,market_context_json,created_at
FROM research_asset_views
"""

_CAUSAL_LINKS_V3_DDL = """
CREATE TABLE causal_links_v3 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id INTEGER NOT NULL,
    chain_json TEXT NOT NULL,
    confidence REAL NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'tracking',
    topic TEXT NOT NULL DEFAULT '',
    supersedes_id INTEGER,
    created_at REAL NOT NULL
)
"""

# 三态映射：superseded 不变；pending+待验证声明→tracking；pending+结论声明→concluded
_CAUSAL_LINKS_V3_COPY = """
INSERT INTO causal_links_v3(id,report_id,chain_json,confidence,evidence_json,status,
    topic,supersedes_id,created_at)
SELECT id,report_id,chain_json,confidence,evidence_json,
    CASE
        WHEN status='superseded' THEN 'superseded'
        WHEN await_verification=1 THEN 'tracking'
        ELSE 'concluded'
    END,
    topic,supersedes_id,created_at
FROM causal_links
"""


async def migrate_research_v3(conn: aiosqlite.Connection) -> None:
    """执行研报表 v3 迁移（幂等）：加列、去死字段、因果链三态化。

    参数：
        conn: aiosqlite.Connection，调用方持有事务的数据库连接；本函数不提交不回滚

    返回：
        None，已是新结构的各步自动跳过

    异常：
        RuntimeError：verify_result 存在非空数据，或 causal_links 存在未知状态/
            非空 broken_at 时（拒绝静默丢弃未知数据）
    """
    if "research_prompt_md5" not in await _table_columns(conn, "research_reports"):
        await conn.execute(
            "ALTER TABLE research_reports ADD COLUMN research_prompt_md5 TEXT NOT NULL DEFAULT ''"
        )
    await _rebuild_asset_views(conn)
    await _rebuild_causal_links(conn)


async def _table_columns(conn: aiosqlite.Connection, table: str) -> set[str]:
    """读取指定表的列名集合（表名仅限本模块代码常量）。

    参数：
        conn: aiosqlite.Connection，数据库连接
        table: str，表名（代码常量）

    返回：
        set[str]：指定表的列名集合
    """
    cur = await conn.execute(f"PRAGMA table_info({table})")  # 表名为代码常量
    return {row["name"] for row in await cur.fetchall()}


async def _count(conn: aiosqlite.Connection, sql: str) -> int:
    """执行 COUNT 查询并返回计数（SQL 仅限本模块代码常量）。

    参数：
        conn: aiosqlite.Connection，数据库连接
        sql: str，COUNT 查询语句（代码常量）

    返回：
        int：查询计数
    """
    cur = await conn.execute(sql)
    row = await cur.fetchone()
    return int(row["n"]) if row else 0


async def _rebuild_asset_views(conn: aiosqlite.Connection) -> None:
    """重建 research_asset_views 去除 verify_result 死字段（无该列时跳过）。

    参数：
        conn: aiosqlite.Connection，调用方持有事务的数据库连接；本函数不提交不回滚

    返回：
        None，旧结构时重建为新结构并保留全部数据，两个索引随表重建

    异常：
        RuntimeError：verify_result 存在非空数据时（死字段无写入路径，非空即未知来源）
    """
    if "verify_result" not in await _table_columns(conn, "research_asset_views"):
        return
    count = await _count(
        conn, "SELECT COUNT(*) AS n FROM research_asset_views WHERE verify_result != ''"
    )
    if count:
        raise RuntimeError(
            f"research_asset_views.verify_result 存在 {count} 条非空数据；"
            "当前版本将移除该死字段，请先备份数据库再启动以完成迁移"
        )
    # SAVEPOINT 包裹 DDL→COPY→DROP→RENAME→INDEX：中途失败回滚到保存点，
    # 旧表保持完整，下次启动可安全重试（issue #113 F8）
    await conn.execute("SAVEPOINT rebuild_asset_views_v3")
    try:
        await conn.execute(_ASSET_VIEWS_V3_DDL)
        await conn.execute(_ASSET_VIEWS_V3_COPY)
        await conn.execute("DROP TABLE research_asset_views")
        await conn.execute("ALTER TABLE research_asset_views_v3 RENAME TO research_asset_views")
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_research_asset_report "
            "ON research_asset_views(report_id)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_research_asset_contract "
            "ON research_asset_views(contract, created_at DESC)"
        )
    except Exception:
        await conn.execute("ROLLBACK TO rebuild_asset_views_v3")
        await conn.execute("RELEASE rebuild_asset_views_v3")
        raise
    await conn.execute("RELEASE rebuild_asset_views_v3")


async def _rebuild_causal_links(conn: aiosqlite.Connection) -> None:
    """重建 causal_links 为三态结构（无 await_verification/broken_at 列时跳过）。

    参数：
        conn: aiosqlite.Connection，调用方持有事务的数据库连接；本函数不提交不回滚

    返回：
        None，旧结构时按三态映射重建并保留全部数据，report 索引随表重建
        （supersedes 索引由调用方在迁移末尾统一重建）

    异常：
        RuntimeError：存在 pending/superseded 之外的状态（如 verified/failed），
            或 broken_at 存在非空数据时（当前版本无对应写入路径，属未知数据）
    """
    if "await_verification" not in await _table_columns(conn, "causal_links"):
        return
    bad_status = await _count(
        conn, "SELECT COUNT(*) AS n FROM causal_links WHERE status NOT IN ('pending','superseded')"
    )
    if bad_status:
        raise RuntimeError(
            f"causal_links 存在 {bad_status} 条未知状态（非 pending/superseded，如 "
            "verified/failed）；当前版本无对应写入路径，请先备份数据库再启动以完成迁移"
        )
    broken = await _count(
        conn, "SELECT COUNT(*) AS n FROM causal_links WHERE broken_at IS NOT NULL"
    )
    if broken:
        raise RuntimeError(
            f"causal_links.broken_at 存在 {broken} 条非空数据；"
            "当前版本将移除该列，请先备份数据库再启动以完成迁移"
        )
    # SAVEPOINT 包裹 DDL→COPY→DROP→RENAME→INDEX：中途失败回滚到保存点，
    # 旧表保持完整，下次启动可安全重试（issue #113 F8）
    await conn.execute("SAVEPOINT rebuild_causal_links_v3")
    try:
        await conn.execute(_CAUSAL_LINKS_V3_DDL)
        await conn.execute(_CAUSAL_LINKS_V3_COPY)
        await conn.execute("DROP TABLE causal_links")
        await conn.execute("ALTER TABLE causal_links_v3 RENAME TO causal_links")
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_causal_links_report ON causal_links(report_id)"
        )
    except Exception:
        await conn.execute("ROLLBACK TO rebuild_causal_links_v3")
        await conn.execute("RELEASE rebuild_causal_links_v3")
        raise
    await conn.execute("RELEASE rebuild_causal_links_v3")
