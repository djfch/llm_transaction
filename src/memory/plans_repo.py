"""交易计划子仓库：trade_plans 表的存取方法（子仓库模式，对齐 review_repo.py）。

PlansRepo 与 Repo 共享同一 Database（同一连接、同一事务语义），由 Repo.__init__
挂载为 repo.plans，交易计划相关调用一律走 repo.plans.xxx；
本模块只依赖 db/models（不反向 import Repo），无循环依赖。

不变量：每合约至多一个 active 计划——save_plan 先把该合约旧 active 置
cancelled（closed_reason='被新计划替代'）再插入，避免同合约计划打架。
"""

from __future__ import annotations

import time

import aiosqlite

from src.memory.db import Database
from src.memory.models import TradePlan
from src.memory.review_repo import query_page_rows, row_without_total


def _now() -> float:
    return time.time()


# 单条 CTE 同时取得列表与总数（与 notes/decisions 分页同口径）；按状态过滤为另一变体
_PLANS_PAGE_SQL = """
WITH total AS (SELECT COUNT(*) AS value FROM trade_plans),
page AS (SELECT * FROM trade_plans ORDER BY id DESC LIMIT ? OFFSET ?)
SELECT page.*, total.value AS total
FROM total LEFT JOIN page ON 1 = 1
ORDER BY page.id DESC
"""
_PLANS_PAGE_BY_STATUS_SQL = """
WITH total AS (SELECT COUNT(*) AS value FROM trade_plans WHERE status = :status),
page AS (
    SELECT * FROM trade_plans WHERE status = :status ORDER BY id DESC LIMIT :limit OFFSET :offset
)
SELECT page.*, total.value AS total
FROM total LEFT JOIN page ON 1 = 1
ORDER BY page.id DESC
"""


class PlansRepo:
    """trade_plans 存取方法集合。所有写操作立即 commit。"""

    def __init__(self, db: Database) -> None:
        self._db = db

    @property
    def _conn(self) -> aiosqlite.Connection:
        return self._db.conn

    async def save_plan(
        self,
        round_id: str,
        contract: str,
        direction: str,
        entry: str,
        stop_loss: str,
        take_profit: str,
        condition: str,
        size_hint: str = "",
        rationale: str = "",
        expires_at: float | None = None,
    ) -> TradePlan:
        """立/换计划：同合约旧 active 自动置 cancelled 后插入新计划（同连接顺序执行）。"""
        ts = _now()
        await self._conn.execute(
            "UPDATE trade_plans SET status='cancelled', closed_reason='被新计划替代',"
            " updated_at=? WHERE contract=? AND status='active'",
            (ts, contract),
        )
        cur = await self._conn.execute(
            "INSERT INTO trade_plans(round_id,contract,direction,entry,stop_loss,take_profit,"
            "size_hint,condition,rationale,expires_at,status,closed_reason,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,'active','',?,?)",
            (
                round_id,
                contract,
                direction,
                entry,
                stop_loss,
                take_profit,
                size_hint,
                condition,
                rationale,
                expires_at,
                ts,
                ts,
            ),
        )
        await self._conn.commit()
        return TradePlan(
            id=cur.lastrowid or 0,
            round_id=round_id,
            contract=contract,
            direction=direction,
            entry=entry,
            stop_loss=stop_loss,
            take_profit=take_profit,
            size_hint=size_hint,
            condition=condition,
            rationale=rationale,
            expires_at=expires_at,
            created_at=ts,
            updated_at=ts,
        )

    async def get_plan(self, plan_id: int) -> TradePlan | None:
        cur = await self._conn.execute("SELECT * FROM trade_plans WHERE id=?", (plan_id,))
        row = await cur.fetchone()
        return TradePlan(**dict(row)) if row else None

    async def close_plan(self, plan_id: int, status: str, reason: str) -> TradePlan | None:
        """收尾计划（executed/cancelled）；仅 active 可关，否则返回 None（由调用方措辞）。"""
        ts = _now()
        cur = await self._conn.execute(
            "UPDATE trade_plans SET status=?, closed_reason=?, updated_at=?"
            " WHERE id=? AND status='active'",
            (status, reason, ts, plan_id),
        )
        await self._conn.commit()
        if cur.rowcount == 0:
            return None
        return await self.get_plan(plan_id)

    async def active_plans(self) -> list[TradePlan]:
        """全部 active 计划（按创建先后），供决策上下文注入。"""
        cur = await self._conn.execute(
            "SELECT * FROM trade_plans WHERE status='active' ORDER BY id"
        )
        rows = await cur.fetchall()
        return [TradePlan(**dict(r)) for r in rows]

    async def list_plans_page(
        self, limit: int, offset: int, status: str | None = None
    ) -> tuple[list[TradePlan], int]:
        """分页（最新在前）+ 总数；status 为 None 返回全部状态。"""
        if status is None:
            rows, total = await query_page_rows(self._conn, _PLANS_PAGE_SQL, limit, offset)
        else:
            cur = await self._conn.execute(
                _PLANS_PAGE_BY_STATUS_SQL, {"status": status, "limit": limit, "offset": offset}
            )
            raw = await cur.fetchall()
            total = int(raw[0]["total"]) if raw else 0
            rows = [r for r in raw if r["id"] is not None]
        return [TradePlan(**row_without_total(r)) for r in rows], total
