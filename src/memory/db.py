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
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id TEXT NOT NULL,
    contract TEXT NOT NULL,
    direction TEXT NOT NULL,
    price TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
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
CREATE INDEX IF NOT EXISTS idx_decisions_created ON decisions(created_at);
CREATE INDEX IF NOT EXISTS idx_trades_created ON trades(created_at);
CREATE INDEX IF NOT EXISTS idx_orders_round ON orders(round_id);
CREATE INDEX IF NOT EXISTS idx_tool_calls_round ON audit_tool_calls(round_id);
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
        """轻量迁移：为已存在的 orders 表补 is_close 列并回填历史平仓单（幂等）。

        历史 close 单落库时 side_size 恒为 '0'，据此回填 is_close=1；
        历史 reduce_only 单无法事后识别，保持 0（只会多计下单数，影响有界）。
        """
        cur = await self._conn.execute("PRAGMA table_info(orders)")
        if "is_close" in {row["name"] for row in await cur.fetchall()}:
            return
        await self._conn.execute(
            "ALTER TABLE orders ADD COLUMN is_close INTEGER NOT NULL DEFAULT 0"
        )
        await self._conn.execute("UPDATE orders SET is_close=1 WHERE side_size='0'")

    async def close(self) -> None:
        """关闭连接；重复调用安全。"""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
