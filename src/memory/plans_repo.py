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

# 计划全文长度上限：每轮进决策上下文，必须有界；不变量与数据同层（工具层另有友好报错）
MAX_PLAN_CHARS = 4000


class PlansRepo:
    """trade_plan 存取方法集合。所有写操作立即 commit。"""

    def __init__(self, db: Database) -> None:
        """挂载共享数据库实例，子仓库与主 Repo 共用同一连接。

        参数：
            db: Database，主仓库持有的数据库实例（同一连接、同一事务语义）

        返回：
            None，仅保存引用到实例属性
        """
        self._db = db

    @property
    def _conn(self) -> aiosqlite.Connection:
        """取底层共享的 SQLite 连接，供本仓库各方法执行 SQL。

        参数：无

        返回：
            aiosqlite.Connection：Database 持有的活动连接
        """
        return self._db.conn

    async def get_plan(self) -> TradePlan | None:
        """当前计划；无行或 content 为空串均视为「无计划」返回 None。

        参数：无

        返回：
            TradePlan | None，当前计划；无行或 content 为空串均视为「无计划」返回 None
        """
        cur = await self._conn.execute("SELECT * FROM trade_plan WHERE id=1")
        row = await cur.fetchone()
        if row is None or not row["content"]:
            return None
        return TradePlan(
            round_id=row["round_id"], content=row["content"], updated_at=row["updated_at"]
        )

    async def save_plan(self, round_id: str, content: str) -> TradePlan:
        """全文覆盖更新（UPSERT 单行）；超长直接拒绝（防绕过工具层击穿上下文有界性）。

        参数：
            round_id: str，决策轮编号
            content: str，计划全文

        返回：
            TradePlan，全文覆盖更新（UPSERT 单行）；超长直接拒绝（防绕过工具层击穿上下文有界性）

        异常：
            ValueError，计划全文超过最大字符数时抛出
        """
        if len(content) > MAX_PLAN_CHARS:
            raise ValueError(f"计划全文超长（{len(content)} > {MAX_PLAN_CHARS} 字符）")
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
        """清空计划（content 置空串 = 无计划）；清空原因经工具调用参数留在审计里。

        参数：
            round_id: str，决策轮编号

        返回：
            None，清空计划（content 置空串 = 无计划）；清空原因经工具调用参数留在审计里
        """
        await self.save_plan(round_id, "")
