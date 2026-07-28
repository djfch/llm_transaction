"""复盘子仓库：策略书版本 / 复盘报告 / 复盘统计取数三组存取方法（从 repo.py 拆出）。

子仓库模式：ReviewRepo 与 Repo 共享同一 Database（同一连接、同一事务语义），由
Repo.__init__ 挂载为 repo.review，复盘相关调用一律走 repo.review.xxx；
本模块只依赖 db/models（不反向 import Repo），无循环依赖。
分页 CTE 助手为模块级函数，供本类与 Repo 的分页方法共用。
"""

from __future__ import annotations

import time

import aiosqlite

from src.memory.db import Database
from src.memory.models import Decision, ReviewReport, StrategyVersion, Trade


def _now() -> float:
    return time.time()


# 单条 CTE 同时取得列表与总数，避免两次 SELECT 被新写入穿插而产生不一致的分页响应。
_REVIEW_REPORTS_PAGE_SQL = """
WITH total AS (SELECT COUNT(*) AS value FROM review_reports),
page AS (SELECT * FROM review_reports ORDER BY id DESC LIMIT ? OFFSET ?)
SELECT page.*, total.value AS total
FROM total LEFT JOIN page ON 1 = 1
ORDER BY page.id DESC
"""


async def query_page_rows(
    conn: aiosqlite.Connection, sql: str, limit: int, offset: int
) -> tuple[list[aiosqlite.Row], int]:
    """执行固定分页 CTE；空页的 LEFT JOIN 占位行仅用于携带 total(总数)。"""
    cur = await conn.execute(sql, (limit, offset))
    raw_rows = await cur.fetchall()
    total = int(raw_rows[0]["total"]) if raw_rows else 0
    return [row for row in raw_rows if row["id"] is not None], total


def row_without_total(row: aiosqlite.Row) -> dict[str, object]:
    """移除分页 CTE 附带的 total(总数) 列，保留领域模型的原始字段。"""
    data = dict(row)
    data.pop("total", None)
    return data


class ReviewRepo:
    """策略版本/复盘报告/复盘取数的存取方法集合。所有写操作立即 commit。"""

    def __init__(self, db: Database) -> None:
        self._db = db

    @property
    def _conn(self) -> aiosqlite.Connection:
        return self._db.conn

    # ---------- strategy_versions（策略书版本） ----------

    async def save_strategy_version(
        self,
        content: str,
        md5: str,
        created_by: str,
        reason: str,
        report_id: int | None = None,
    ) -> StrategyVersion:
        """落库一个策略书版本（content 为完整原文，md5 为关联键）。"""
        ts = _now()
        cur = await self._conn.execute(
            "INSERT INTO strategy_versions(content,md5,created_by,reason,report_id,created_at)"
            " VALUES(?,?,?,?,?,?)",
            (content, md5, created_by, reason, report_id, ts),
        )
        await self._conn.commit()
        return StrategyVersion(
            id=cur.lastrowid or 0,
            content=content,
            md5=md5,
            created_by=created_by,
            reason=reason,
            report_id=report_id,
            created_at=ts,
        )

    async def list_strategy_versions(self) -> list[StrategyVersion]:
        """全部版本，按 id 倒序（最新在前）。"""
        cur = await self._conn.execute("SELECT * FROM strategy_versions ORDER BY id DESC")
        return [StrategyVersion(**dict(r)) for r in await cur.fetchall()]

    async def get_strategy_version(self, version_id: int) -> StrategyVersion | None:
        cur = await self._conn.execute("SELECT * FROM strategy_versions WHERE id=?", (version_id,))
        row = await cur.fetchone()
        return StrategyVersion(**dict(row)) if row else None

    async def attach_report_to_version(self, version_id: int, report_id: int) -> None:
        """回填触发该版本的复盘报告 id（版本先落库、报告后落库的反向关联）。"""
        await self._conn.execute(
            "UPDATE strategy_versions SET report_id=? WHERE id=?", (report_id, version_id)
        )
        await self._conn.commit()

    # ---------- review_reports（复盘报告） ----------

    async def save_review_report(
        self,
        period_start: float,
        period_end: float,
        stats_json: str,
        report_md: str,
        strategy_action: str,
        new_version_id: int | None = None,
        error: str = "",
    ) -> ReviewReport:
        """落库一份复盘报告；error 非空表示该次复盘失败（只留错误记录）。"""
        ts = _now()
        cur = await self._conn.execute(
            "INSERT INTO review_reports(period_start,period_end,stats_json,report_md,"
            "strategy_action,new_version_id,error,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                period_start,
                period_end,
                stats_json,
                report_md,
                strategy_action,
                new_version_id,
                error,
                ts,
            ),
        )
        await self._conn.commit()
        return ReviewReport(
            id=cur.lastrowid or 0,
            period_start=period_start,
            period_end=period_end,
            stats_json=stats_json,
            report_md=report_md,
            strategy_action=strategy_action,
            new_version_id=new_version_id,
            error=error,
            created_at=ts,
        )

    async def list_review_reports_page(
        self, limit: int, offset: int
    ) -> tuple[list[ReviewReport], int]:
        """以单条 SQL 快照返回报告页及总数，越界页仍保留准确总数。"""
        rows, total = await query_page_rows(self._conn, _REVIEW_REPORTS_PAGE_SQL, limit, offset)
        return [ReviewReport(**row_without_total(row)) for row in rows], total

    async def get_review_report(self, report_id: int) -> ReviewReport | None:
        cur = await self._conn.execute("SELECT * FROM review_reports WHERE id=?", (report_id,))
        row = await cur.fetchone()
        return ReviewReport(**dict(row)) if row else None

    async def latest_review_period_end(self) -> float | None:
        """最近一次复盘的 period_end；无记录返回 None（调度幂等：不重复复盘同一区间）。"""
        cur = await self._conn.execute("SELECT MAX(period_end) AS value FROM review_reports")
        row = await cur.fetchone()
        value = row["value"] if row else None
        return float(value) if value is not None else None

    # ---------- 复盘统计取数 ----------

    async def trades_for_review(
        self,
        start_ts: float,
        end_ts: float,
        mode: str,
        contract: str | None = None,
        strategy_md5: str | None = None,
    ) -> list[Trade]:
        """区间内成交（[start, end)，按 id 正序），LEFT JOIN decisions 支持按策略版本过滤。

        mode 必填过滤；strategy_md5/contract 非空时分别加对应过滤。
        口径（spec §6）：无 strategy_md5 过滤时基础样本只按 source/mode 过滤，孤儿平仓成交
        （round_id=''，decisions 无匹配，如 LLM 未配置期间 drain 的强平/止盈止损）也计入；
        仅按策略统计时无 join 匹配的成交不参与（decisions.strategy_md5=? 自然排除 NULL 行，
        与 INNER JOIN 语义一致）。
        """
        sql = (
            "SELECT trades.* FROM trades"
            " LEFT JOIN decisions ON trades.round_id = decisions.round_id"
            " WHERE trades.created_at >= ? AND trades.created_at < ? AND trades.mode=?"
        )
        params: list = [start_ts, end_ts, mode]
        if strategy_md5:
            sql += " AND decisions.strategy_md5=?"
            params.append(strategy_md5)
        if contract:
            sql += " AND trades.contract=?"
            params.append(contract)
        cur = await self._conn.execute(sql + " ORDER BY trades.id", params)
        return [Trade(**dict(r)) for r in await cur.fetchall()]

    async def decisions_for_review(
        self,
        start_ts: float,
        end_ts: float,
        strategy_md5: str | None = None,
        limit: int = 100,
        mode: str | None = None,
    ) -> list[Decision]:
        """区间内决策（[start, end)，按 id 倒序）；limit 钳制到 1..100；mode 非空时按模式过滤。"""
        limit = max(1, min(100, limit))
        sql = "SELECT * FROM decisions WHERE created_at >= ? AND created_at < ?"
        params: list = [start_ts, end_ts]
        if strategy_md5:
            sql += " AND strategy_md5=?"
            params.append(strategy_md5)
        if mode is not None:
            sql += " AND mode=?"
            params.append(mode)
        params.append(limit)
        cur = await self._conn.execute(sql + " ORDER BY id DESC LIMIT ?", params)
        return [Decision(**dict(r)) for r in await cur.fetchall()]

    async def list_trades_filtered(
        self,
        start_ts: float,
        end_ts: float,
        contract: str | None = None,
        source: str | None = None,
        limit: int = 200,
        mode: str | None = None,
    ) -> list[Trade]:
        """区间内成交（[start, end)，按 id 正序）；contract/source/mode 可选过滤，limit 钳 1..200。"""
        limit = max(1, min(200, limit))
        sql = "SELECT * FROM trades WHERE created_at >= ? AND created_at < ?"
        params: list = [start_ts, end_ts]
        if contract:
            sql += " AND contract=?"
            params.append(contract)
        if source is not None:
            sql += " AND source=?"
            params.append(source)
        if mode is not None:
            sql += " AND mode=?"
            params.append(mode)
        params.append(limit)
        cur = await self._conn.execute(sql + " ORDER BY id LIMIT ?", params)
        return [Trade(**dict(r)) for r in await cur.fetchall()]
