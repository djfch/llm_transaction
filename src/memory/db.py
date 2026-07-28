"""SQLite 持久化：aiosqlite 连接管理（open/close、WAL 模式）与建表。

约定：金额/数量字段以 TEXT 存 Decimal 字符串，避免浮点误差；时间字段为 Unix 秒（REAL）。
"""

from __future__ import annotations

from pathlib import Path

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    strategy_version TEXT NOT NULL DEFAULT '',
    wake_source TEXT NOT NULL DEFAULT '',
    context_summary TEXT NOT NULL DEFAULT '',
    llm_raw TEXT NOT NULL DEFAULT '',
    strategy_md5 TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    round_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    contract TEXT NOT NULL,
    side_size TEXT NOT NULL,
    price TEXT NOT NULL DEFAULT '',
    tif TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    finish_as TEXT NOT NULL DEFAULT '',
    is_close INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    contract TEXT NOT NULL,
    size TEXT NOT NULL,
    price TEXT NOT NULL,
    fee TEXT NOT NULL,
    pnl TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS wakeup (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scheduled_at REAL NOT NULL,
    source TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_rounds (
    round_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    wake_source TEXT NOT NULL DEFAULT '',
    prompt_md5 TEXT NOT NULL DEFAULT '',
    strategy_md5 TEXT NOT NULL DEFAULT '',
    prompt_snapshot TEXT NOT NULL DEFAULT '',
    context_snapshot TEXT NOT NULL DEFAULT '',
    llm_raw TEXT NOT NULL DEFAULT '',
    started_at REAL NOT NULL,
    ended_at REAL,
    error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS audit_tool_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    tool TEXT NOT NULL,
    args_json TEXT NOT NULL DEFAULT '{}',
    risk_verdict TEXT NOT NULL DEFAULT '',
    risk_reason TEXT NOT NULL DEFAULT '',
    result_json TEXT NOT NULL DEFAULT '{}',
    duration_ms INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS strategy_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT NOT NULL,
    md5 TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    report_id INTEGER,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS review_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    period_start REAL NOT NULL,
    period_end REAL NOT NULL,
    stats_json TEXT NOT NULL DEFAULT '{}',
    report_md TEXT NOT NULL DEFAULT '',
    strategy_action TEXT NOT NULL DEFAULT 'none',
    new_version_id INTEGER,
    error TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decisions_created ON decisions(created_at);
CREATE INDEX IF NOT EXISTS idx_trades_created ON trades(created_at);
CREATE INDEX IF NOT EXISTS idx_orders_round ON orders(round_id);
CREATE INDEX IF NOT EXISTS idx_tool_calls_round ON audit_tool_calls(round_id);
CREATE INDEX IF NOT EXISTS idx_strategy_versions_md5 ON strategy_versions(md5);
CREATE INDEX IF NOT EXISTS idx_review_reports_created ON review_reports(created_at);
"""


class Database:
    """aiosqlite 连接封装：open 时启用 WAL 并建表，close 释放连接。"""

    def __init__(self) -> None:
        self._conn: aiosqlite.Connection | None = None

    @property
    def conn(self) -> aiosqlite.Connection:
        """已打开的连接；未 open 时抛错，防止隐式依赖未初始化状态。"""
        if self._conn is None:
            raise RuntimeError("数据库未打开，请先调用 open()")
        return self._conn

    async def open(self, path: str | Path) -> None:
        """打开（必要时创建）数据库文件，启用 WAL 模式并执行建表与轻量迁移。"""
        db_path = Path(path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(str(db_path))
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.executescript(SCHEMA)
        await self._migrate()
        await self._conn.commit()

    async def _migrate(self) -> None:
        """轻量迁移（均幂等，用 PRAGMA table_info 判列存在性）：

        - orders.is_close：历史 close 单落库时 side_size 恒为 '0'，据此回填 is_close=1；
          历史 reduce_only 单无法事后识别，保持 0（只会多计下单数，影响有界）。
        - trades.source：历史成交无法可靠推断来源，保持默认 ''（历史/未知），不回填。
        - decisions/audit_rounds.strategy_md5：历史数据无策略书原文 md5 可循，
          保持默认 ''（不与任何版本关联），不回填。
          strategy_versions/review_reports 为新增表，由 CREATE TABLE IF NOT EXISTS 覆盖。
        """
        cur = await self._conn.execute("PRAGMA table_info(orders)")
        if "is_close" not in {row["name"] for row in await cur.fetchall()}:
            await self._conn.execute(
                "ALTER TABLE orders ADD COLUMN is_close INTEGER NOT NULL DEFAULT 0"
            )
            await self._conn.execute("UPDATE orders SET is_close=1 WHERE side_size='0'")
        cur = await self._conn.execute("PRAGMA table_info(trades)")
        if "source" not in {row["name"] for row in await cur.fetchall()}:
            await self._conn.execute(
                "ALTER TABLE trades ADD COLUMN source TEXT NOT NULL DEFAULT ''"
            )
        for table in ("decisions", "audit_rounds"):
            cur = await self._conn.execute(f"PRAGMA table_info({table})")  # 表名为代码常量
            if "strategy_md5" not in {row["name"] for row in await cur.fetchall()}:
                await self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN strategy_md5 TEXT NOT NULL DEFAULT ''"
                )

    async def close(self) -> None:
        """关闭连接；重复调用安全。"""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
