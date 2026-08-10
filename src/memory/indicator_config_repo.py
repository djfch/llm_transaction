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
    """返回当前 Unix 时间戳（秒），用作版本行的 created_at。

    参数：无

    返回：
        float：当前时间的 Unix 秒时间戳
    """
    return time.time()


def _row_to_version(row: aiosqlite.Row) -> IndicatorConfigVersion:
    """把 indicator_config_versions 表的一行查询结果转换为版本对象。

    参数：
        row: aiosqlite.Row，indicator_config_versions 表的单行查询结果

    返回：
        IndicatorConfigVersion：由该行各字段构造的指标短名单版本对象
    """
    return IndicatorConfigVersion(**dict(row))


class IndicatorConfigRepo:
    """指标短名单版本存取方法集合。所有写操作立即 commit。"""

    def __init__(self, db: Database) -> None:
        """初始化子仓库，持有与 Repo 共享的数据库句柄。

        参数：
            db: Database，与 Repo 共享的数据库句柄（同一连接、同一事务语义）

        返回：
            None，初始化实例属性（self._db）
        """
        self._db = db

    @property
    def _conn(self) -> aiosqlite.Connection:
        """返回共享数据库连接，供本仓库各方法执行 SQL。

        参数：无

        返回：
            aiosqlite.Connection：与 Repo 共享的同一数据库连接
        """
        return self._db.conn

    async def save_version(
        self,
        content: str,
        md5: str,
        created_by: str,
        reason: str,
        report_id: int | None = None,
    ) -> IndicatorConfigVersion:
        """保存一个指标短名单版本并立即提交，返回包含新编号的完整版本对象。

        参数：
            content: str，指标短名单配置原文
            md5: str，关联决策轮与配置内容的摘要
            created_by: str，版本创建者分类
            reason: str，本次修订原因
            report_id: int | None，触发本次版本的复盘报告编号

        返回：
            IndicatorConfigVersion，包含数据库编号与创建时间的新版本对象
        """
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
        """按版本编号倒序读取指标短名单历史，并把条数限制钳制到安全范围。

        参数：
            limit: int，期望返回条数，实际限制在 1 到 200 之间

        返回：
            list[IndicatorConfigVersion]，最新版本在前的指标短名单版本列表
        """
        limit = max(1, min(200, limit))
        cur = await self._conn.execute(
            "SELECT * FROM indicator_config_versions ORDER BY id DESC LIMIT ?", (limit,)
        )
        return [_row_to_version(r) for r in await cur.fetchall()]

    async def get_version(self, version_id: int) -> IndicatorConfigVersion | None:
        """按 id 读取单个指标短名单版本。

        参数：
            version_id: int，版本行 id

        返回：
            IndicatorConfigVersion | None：对应版本对象；无该 id 时返回 None
        """
        cur = await self._conn.execute(
            "SELECT * FROM indicator_config_versions WHERE id=?", (version_id,)
        )
        row = await cur.fetchone()
        return _row_to_version(row) if row else None

    async def latest_version(self) -> IndicatorConfigVersion | None:
        """读取最近创建的指标短名单版本。

        参数：无

        返回：
            IndicatorConfigVersion | None，最新版本对象；版本表为空时返回 None
        """
        cur = await self._conn.execute(
            "SELECT * FROM indicator_config_versions ORDER BY id DESC LIMIT 1"
        )
        row = await cur.fetchone()
        return _row_to_version(row) if row else None

    async def latest_md5(self) -> str | None:
        """读取最新指标短名单版本的内容摘要，供决策轮关联配置版本。

        参数：无

        返回：
            str | None，最新版本的 MD5 摘要；无版本时返回 None
        """
        version = await self.latest_version()
        return version.md5 if version else None

    async def attach_report_to_version(self, version_id: int, report_id: int) -> None:
        """在复盘报告落库后把其编号回填到先创建的指标短名单版本。

        参数：
            version_id: int，待关联的指标短名单版本编号
            report_id: int，触发该版本的复盘报告编号

        返回：
            None，更新版本行并立即提交数据库事务
        """
        await self._conn.execute(
            "UPDATE indicator_config_versions SET report_id=? WHERE id=?", (report_id, version_id)
        )
        await self._conn.commit()
