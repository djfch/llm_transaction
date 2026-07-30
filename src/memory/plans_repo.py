"""交易计划子仓库：trade_plan 单行表的存取（子仓库模式，对齐 review_repo.py）。

PlansRepo 与 Repo 共享同一 Database（同一连接、同一事务语义），由 Repo.__init__
挂载为 repo.plans；本模块只依赖 db/models（不反向 import Repo），无循环依赖。

设计：全局唯一一份计划——表用 CHECK(id=1) 固定单行，update 即 UPSERT 全文覆盖，
clear 置空 content；历史不单独留表（每轮审计上下文快照已冻结当轮计划原文）。
"""

from __future__ import annotations

import time

import aiosqlite

from src.memory.db import Database
from src.memory.models import TradePlan


class PlansRepo:
    """trade_plan 存取方法集合。所有写操作立即 commit。"""

    def __init__(self, db: Database) -> None:
        self._db = db

    @property
    def _conn(self) -> aiosqlite.Connection:
        return self._db.conn

    async def get_plan(self) -> TradePlan | None:
        """当前计划；无行或 content 为空串均视为「无计划」返回 None。"""
        cur = await self._conn.execute("SELECT * FROM trade_plan WHERE id=1")
        row = await cur.fetchone()
        if row is None or not row["content"]:
            return None
        return TradePlan(
            round_id=row["round_id"], content=row["content"], updated_at=row["updated_at"]
        )

    async def save_plan(self, round_id: str, content: str) -> TradePlan:
        """全文覆盖更新（UPSERT 单行）。"""
        ts = time.time()
        await self._conn.execute(
            "INSERT INTO trade_plan(id, round_id, content, updated_at) VALUES(1,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET round_id=excluded.round_id,"
            " content=excluded.content, updated_at=excluded.updated_at",
            (round_id, content, ts),
        )
        await self._conn.commit()
        return TradePlan(round_id=round_id, content=content, updated_at=ts)

    async def clear_plan(self, round_id: str) -> None:
        """清空计划（content 置空串 = 无计划）；清空原因经工具调用参数留在审计里。"""
        await self.save_plan(round_id, "")
