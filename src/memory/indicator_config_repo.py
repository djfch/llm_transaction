"""指标短名单版本子仓库：indicator_config_versions 表的存取（子仓库模式，对齐 review_repo.py）。

IndicatorConfigRepo 与 Repo 共享同一 Database（同一连接、同一事务语义），由
Repo.__init__ 挂载为 repo.indicator_config；本模块只依赖 db/models（不反向 import
Repo），无循环依赖。表结构与 strategy_versions 一致（content 为配置原文，md5 为关联键）。
"""

from __future__ import annotations

import time

import aiosqlite

from src.memory.db import Database
from src.memory.models import IndicatorConfigVersion


def _now() -> float:
    return time.time()


def _row_to_version(row: aiosqlite.Row) -> IndicatorConfigVersion:
    return IndicatorConfigVersion(**dict(row))


class IndicatorConfigRepo:
    """指标短名单版本存取方法集合。所有写操作立即 commit。"""

    def __init__(self, db: Database) -> None:
        self._db = db

    @property
    def _conn(self) -> aiosqlite.Connection:
        return self._db.conn

    async def save_version(
        self,
        content: str,
        md5: str,
        created_by: str,
        reason: str,
        report_id: int | None = None,
    ) -> IndicatorConfigVersion:
        """落库一个短名单版本（content 为配置原文，md5 为关联键），返回含 id 的完整版本行。"""
        ts = _now()
        cur = await self._conn.execute(
            "INSERT INTO indicator_config_versions(content,md5,created_by,reason,report_id,"
            "created_at) VALUES(?,?,?,?,?,?)",
            (content, md5, created_by, reason, report_id, ts),
        )
        await self._conn.commit()
        return IndicatorConfigVersion(
            id=cur.lastrowid or 0,
            content=content,
            md5=md5,
            created_by=created_by,
            reason=reason,
            report_id=report_id,
            created_at=ts,
        )

    async def list_versions(self, limit: int = 50) -> list[IndicatorConfigVersion]:
        """版本列表，按 id 倒序（最新在前）；limit 钳制到 1..200。"""
        limit = max(1, min(200, limit))
        cur = await self._conn.execute(
            "SELECT * FROM indicator_config_versions ORDER BY id DESC LIMIT ?", (limit,)
        )
        return [_row_to_version(r) for r in await cur.fetchall()]

    async def get_version(self, version_id: int) -> IndicatorConfigVersion | None:
        cur = await self._conn.execute(
            "SELECT * FROM indicator_config_versions WHERE id=?", (version_id,)
        )
        row = await cur.fetchone()
        return _row_to_version(row) if row else None

    async def latest_version(self) -> IndicatorConfigVersion | None:
        """最新版本；无记录返回 None。"""
        cur = await self._conn.execute(
            "SELECT * FROM indicator_config_versions ORDER BY id DESC LIMIT 1"
        )
        row = await cur.fetchone()
        return _row_to_version(row) if row else None

    async def latest_md5(self) -> str | None:
        """最新版本的 md5（供决策轮与配置版本关联）；无记录返回 None。"""
        version = await self.latest_version()
        return version.md5 if version else None

    async def attach_report_to_version(self, version_id: int, report_id: int) -> None:
        """回填触发该版本的复盘报告 id（版本先落库、报告后落库的反向关联）。"""
        await self._conn.execute(
            "UPDATE indicator_config_versions SET report_id=? WHERE id=?", (report_id, version_id)
        )
        await self._conn.commit()
