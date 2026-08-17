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
    trade_source TEXT NOT NULL DEFAULT '',
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
    exchange_trade_id TEXT,
    exchange_order_id TEXT,
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
CREATE TABLE IF NOT EXISTS indicator_config_versions (
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
    round_id TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS trade_plan (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    round_id TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL
);
-- 研报系统三表：事实层 timeline / 判断层 research_reports / 分析笔记 causal_links
CREATE TABLE IF NOT EXISTS timeline (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL DEFAULT '',
    published_at REAL NOT NULL,
    meta_json TEXT NOT NULL DEFAULT '{}',
    dedup_key TEXT NOT NULL UNIQUE,
    fetched_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS research_reports (
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
);
CREATE TABLE IF NOT EXISTS research_schedule_runs (
    schedule_id TEXT NOT NULL,
    scheduled_date TEXT NOT NULL,
    claimed_at REAL NOT NULL,
    PRIMARY KEY(schedule_id, scheduled_date)
);
CREATE TABLE IF NOT EXISTS research_asset_views (
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
);
CREATE TABLE IF NOT EXISTS causal_links (
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
);
CREATE INDEX IF NOT EXISTS idx_decisions_created ON decisions(created_at);
CREATE INDEX IF NOT EXISTS idx_trades_created ON trades(created_at);
CREATE INDEX IF NOT EXISTS idx_orders_round ON orders(round_id);
CREATE INDEX IF NOT EXISTS idx_tool_calls_round ON audit_tool_calls(round_id);
CREATE INDEX IF NOT EXISTS idx_strategy_versions_md5 ON strategy_versions(md5);
CREATE INDEX IF NOT EXISTS idx_review_reports_created ON review_reports(created_at);
CREATE INDEX IF NOT EXISTS idx_timeline_published ON timeline(published_at);
CREATE INDEX IF NOT EXISTS idx_research_reports_created ON research_reports(created_at);
CREATE INDEX IF NOT EXISTS idx_research_asset_report ON research_asset_views(report_id);
CREATE INDEX IF NOT EXISTS idx_research_asset_contract ON research_asset_views(contract, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_causal_links_report ON causal_links(report_id);
"""

_RESEARCH_REPORT_COLUMNS = {
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
_RESEARCH_ASSET_COLUMNS = {
    "id",
    "report_id",
    "contract",
    "direction",
    "confidence",
    "horizon",
    "market_regime",
    "technical_confirmation",
    "basis_type",
    "data_status",
    "evidence_json",
    "risks_json",
    "narrative",
    "market_context_json",
    "verify_result",
    "created_at",
}


class Database:
    """aiosqlite 连接封装：open 时启用 WAL 并建表，close 释放连接。"""

    def __init__(self) -> None:
        """初始化数据库封装实例，连接与文件路径置空（未打开状态）。

        参数：无

        返回：
            None，仅初始化内部字段；真正的连接与建表在 open() 中完成
        """
        self._path: Path | None = None
        self._conn: aiosqlite.Connection | None = None

    @property
    def conn(self) -> aiosqlite.Connection:
        """已打开的连接；未 open 时抛错，防止隐式依赖未初始化状态。

        参数：
            无

        返回：
            aiosqlite.Connection：已打开的连接；未 open 时抛错，防止隐式依赖未初始化状态

        异常：
            RuntimeError：'数据库未打开，请先调用 open()' 所描述的条件发生时
        """
        if self._conn is None:
            raise RuntimeError("数据库未打开，请先调用 open()")
        return self._conn

    async def open(self, path: str | Path) -> None:
        """打开（必要时创建）数据库文件，启用 WAL 模式并执行建表与轻量迁移。

        参数：
            path: str | Path，目标文件或数据库路径

        返回：
            None：打开（必要时创建）数据库文件，启用 WAL 模式并执行建表与轻量迁移

        异常：
            Exception：捕获当前异常后原样重新抛出
        """
        db_path = Path(path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(str(db_path))
        try:
            self._conn.row_factory = aiosqlite.Row
            await self._conn.execute("PRAGMA journal_mode=WAL")
            await self._validate_research_schema()
            await self._conn.executescript(SCHEMA)
            await self._migrate()
            await self._conn.commit()
            self._path = db_path
        except Exception:
            await self._conn.close()
            self._conn = None
            self._path = None
            raise

    async def _validate_research_schema(self) -> None:
        """只接受当前逐标的研报结构；生产基线无研报表时允许直接建表。

        参数：
            无

        返回：
            None：只接受当前逐标的研报结构；生产基线无研报表时允许直接建表

        异常：
            RuntimeError：'检测到旧版研报表 research_reports；当前版本不执行兼容迁移，请先备份数据库并按部署文档重建研报数据' 所描述的条件发生时
            RuntimeError：'研报表结构不完整：缺少 research_asset_views' 所描述的条件发生时
            RuntimeError：'研报表结构不完整：research_asset_views 字段不符合当前协议' 所描述的条件发生时
            RuntimeError：'检测到非 schema_version=2 的研报数据；请先备份数据库并重建研报数据' 所描述的条件发生时
        """
        cur = await self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='research_reports'"
        )
        if await cur.fetchone() is None:
            return
        cur = await self._conn.execute("PRAGMA table_info(research_reports)")
        report_columns = {row["name"] for row in await cur.fetchall()}
        if report_columns != _RESEARCH_REPORT_COLUMNS:
            raise RuntimeError(
                "检测到旧版研报表 research_reports；当前版本不执行兼容迁移，"
                "请先备份数据库并按部署文档重建研报数据"
            )
        cur = await self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='research_asset_views'"
        )
        if await cur.fetchone() is None:
            raise RuntimeError("研报表结构不完整：缺少 research_asset_views")
        cur = await self._conn.execute("PRAGMA table_info(research_asset_views)")
        asset_columns = {row["name"] for row in await cur.fetchall()}
        if asset_columns != _RESEARCH_ASSET_COLUMNS:
            raise RuntimeError("研报表结构不完整：research_asset_views 字段不符合当前协议")
        cur = await self._conn.execute(
            "SELECT 1 FROM research_reports WHERE schema_version != 2 LIMIT 1"
        )
        if await cur.fetchone() is not None:
            raise RuntimeError("检测到非 schema_version=2 的研报数据；请先备份数据库并重建研报数据")

    @property
    def path(self) -> Path:
        """当前数据库文件路径；独立事务连接使用同一路径。

        参数：
            无

        返回：
            Path：当前数据库文件路径；独立事务连接使用同一路径

        异常：
            RuntimeError：'数据库未打开，请先调用 open()' 所描述的条件发生时
        """
        if self._path is None:
            raise RuntimeError("数据库未打开，请先调用 open()")
        return self._path

    async def _migrate(self) -> None:
        """轻量迁移（均幂等，用 PRAGMA table_info 判列存在性）：

        - orders.is_close：历史 close 单落库时 side_size 恒为 '0'，据此回填 is_close=1；
          历史 reduce_only 单无法事后识别，保持 0（只会多计下单数，影响有界）。
        - trades.source：历史成交无法可靠推断来源，保持默认 ''（历史/未知），不回填。
        - decisions/audit_rounds.strategy_md5：历史数据无策略书原文 md5 可循，
          保持默认 ''（不与任何版本关联），不回填。
          strategy_versions/review_reports/indicator_config_versions 为新增表，
          由 CREATE TABLE IF NOT EXISTS 覆盖。
        - orders.trade_source：历史订单来源无法可靠推断，保持默认 ''（成交分类按
          is_close 推导 llm_open/llm_close），不回填。
        - trades.exchange_trade_id：历史成交无交易所成交 id，保持 NULL（部分唯一索引
          不约束 NULL）；索引只在迁移末尾建（旧库须先补列，SCHEMA 阶段建会因缺列报错）。
        - trades.exchange_order_id：历史成交无交易所订单 id，保持 NULL（乱序补正
          只能跳过这些旧行，属可接受残留），不回填。
        - review_reports.round_id：老报告无审计轮可循，保持默认 ''（无关联），不回填。
        - causal_links.topic/supersedes_id/await_verification：旧链无主题（''）、无替代关系
          （NULL）、按待验证处理（1），不回填；supersedes 索引只在迁移末尾建（旧库须先补列，
          SCHEMA 阶段建会因缺列报错，同 trades.exchange_trade_id）。

        参数：
            无

        返回：
            None：轻量迁移（均幂等，用 PRAGMA table_info 判列存在性）：
        """
        cur = await self._conn.execute("PRAGMA table_info(orders)")
        order_cols = {row["name"] for row in await cur.fetchall()}
        if "is_close" not in order_cols:
            await self._conn.execute(
                "ALTER TABLE orders ADD COLUMN is_close INTEGER NOT NULL DEFAULT 0"
            )
            await self._conn.execute("UPDATE orders SET is_close=1 WHERE side_size='0'")
        if "trade_source" not in order_cols:
            await self._conn.execute(
                "ALTER TABLE orders ADD COLUMN trade_source TEXT NOT NULL DEFAULT ''"
            )
        cur = await self._conn.execute("PRAGMA table_info(trades)")
        trade_cols = {row["name"] for row in await cur.fetchall()}
        if "source" not in trade_cols:
            await self._conn.execute(
                "ALTER TABLE trades ADD COLUMN source TEXT NOT NULL DEFAULT ''"
            )
        if "exchange_trade_id" not in trade_cols:
            await self._conn.execute("ALTER TABLE trades ADD COLUMN exchange_trade_id TEXT")
        if "exchange_order_id" not in trade_cols:
            await self._conn.execute("ALTER TABLE trades ADD COLUMN exchange_order_id TEXT")
        await self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_exchange_id "
            "ON trades(exchange_trade_id) WHERE exchange_trade_id IS NOT NULL"
        )
        for table in ("decisions", "audit_rounds"):
            cur = await self._conn.execute(f"PRAGMA table_info({table})")  # 表名为代码常量
            if "strategy_md5" not in {row["name"] for row in await cur.fetchall()}:
                await self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN strategy_md5 TEXT NOT NULL DEFAULT ''"
                )
        cur = await self._conn.execute("PRAGMA table_info(review_reports)")
        if "round_id" not in {row["name"] for row in await cur.fetchall()}:
            await self._conn.execute(
                "ALTER TABLE review_reports ADD COLUMN round_id TEXT NOT NULL DEFAULT ''"
            )
        cur = await self._conn.execute("PRAGMA table_info(causal_links)")
        link_cols = {row["name"] for row in await cur.fetchall()}
        if "topic" not in link_cols:
            await self._conn.execute(
                "ALTER TABLE causal_links ADD COLUMN topic TEXT NOT NULL DEFAULT ''"
            )
        if "supersedes_id" not in link_cols:
            await self._conn.execute("ALTER TABLE causal_links ADD COLUMN supersedes_id INTEGER")
        if "await_verification" not in link_cols:
            await self._conn.execute(
                "ALTER TABLE causal_links ADD COLUMN await_verification INTEGER NOT NULL DEFAULT 1"
            )
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_causal_links_supersedes ON causal_links(supersedes_id)"
        )

    async def close(self) -> None:
        """关闭连接；重复调用安全。

        参数：
            无

        返回：
            None：关闭连接；重复调用安全
        """
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
