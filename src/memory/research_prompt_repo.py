"""研报提示词版本子仓库：research_prompt_versions 存取（issue #113）。

子仓库模式：与 Repo 共享同一 Database（同一连接、同一事务语义），由
Repo.__init__ 挂载为 repo.research_prompt；本模块只依赖 db/models
（不反向 import Repo），无循环依赖。状态机与 strategy_versions 对齐：
applied 已生效 / draft 草稿（复盘报告成功才生效）/ discarded 已废弃。
"""

from __future__ import annotations

import time

import aiosqlite

from src.memory.db import Database
from src.memory.models import ResearchPromptVersion


def _now() -> float:
    """取当前 Unix 时间戳（秒），作为记录的 created_at 落库时间。

    参数：无

    返回：
        float：当前 Unix 时间戳（秒）
    """
    return time.time()


class ResearchPromptRepo:
    """研报提示词版本的存取方法集合。所有写操作立即 commit。"""

    def __init__(self, db: Database) -> None:
        """绑定共享数据库句柄（与 Repo 及其他子仓库共用同一连接与事务语义）。

        参数：
            db: Database，已打开的数据库句柄，由 Repo 挂载时传入

        返回：
            None，仅保存数据库引用，不触发任何 IO
        """
        self._db = db

    @property
    def _conn(self) -> aiosqlite.Connection:
        """取共享的 aiosqlite 连接，供本类各存取方法执行 SQL。

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
        review_report_id: int | None = None,
        status: str = "applied",
        base_md5: str | None = None,
    ) -> ResearchPromptVersion:
        """落库一个研报提示词版本（content 为完整原文，md5 为关联键）。

        参数：
            content: str，提示词完整正文
            md5: str，提示词正文摘要（与 research_reports.research_prompt_md5 关联）
            created_by: str，版本创建来源（human/review_agent/rollback）
            reason: str，操作原因
            review_report_id: int | None，触发本版本的复盘报告编号
            status: str，版本状态：applied 已生效 / draft 草稿 / discarded 已废弃
            base_md5: str | None，草稿基线 md5（复盘轮初采样的当时生效内容摘要，
                issue #113 CAS）；None = 人工/历史行，生效判定回退旧 id 比较

        返回：
            ResearchPromptVersion：已落库的版本对象
        """
        ts = _now()
        cur = await self._conn.execute(
            "INSERT INTO research_prompt_versions(content,md5,created_by,reason,"
            "review_report_id,created_at,status,base_md5) VALUES(?,?,?,?,?,?,?,?)",
            (content, md5, created_by, reason, review_report_id, ts, status, base_md5),
        )
        await self._conn.commit()
        return ResearchPromptVersion(
            id=cur.lastrowid or 0,
            content=content,
            md5=md5,
            created_by=created_by,
            reason=reason,
            review_report_id=review_report_id,
            created_at=ts,
            status=status,
            base_md5=base_md5,
        )

    async def list_versions(self) -> list[ResearchPromptVersion]:
        """全部版本，按 id 倒序（最新在前）。

        参数：无

        返回：
            list[ResearchPromptVersion]：全部版本，按 id 倒序（最新在前）
        """
        cur = await self._conn.execute("SELECT * FROM research_prompt_versions ORDER BY id DESC")
        return [ResearchPromptVersion(**dict(r)) for r in await cur.fetchall()]

    async def get_version(self, version_id: int) -> ResearchPromptVersion | None:
        """按 id 读取单个研报提示词版本；不存在返回 None。

        参数：
            version_id: int，版本编号

        返回：
            ResearchPromptVersion | None：命中的版本；id 不存在时返回 None
        """
        cur = await self._conn.execute(
            "SELECT * FROM research_prompt_versions WHERE id=?", (version_id,)
        )
        row = await cur.fetchone()
        return ResearchPromptVersion(**dict(row)) if row else None

    async def get_version_by_md5(
        self, md5: str, *, as_of_ts: float
    ) -> ResearchPromptVersion | None:
        """按正文 md5 反解指定时点最新生效的版本（研报归因展示用，issue #113 R6/V3）。

        只认 status='applied' 且 created_at <= as_of_ts 的版本：draft/discarded
        从未生效，不能作为归因；晚于研报时点的同 md5 版本（如回滚后再生的同文
        版本）不得归因给该研报，否则旧研报的提示词归因会被后来的同名版本篡改。

        参数：
            md5: str，提示词正文摘要（research_reports.research_prompt_md5 的值）
            as_of_ts: float，归因时点（通常取被归因研报的 created_at）

        返回：
            ResearchPromptVersion | None：该时点该 md5 最新生效的版本；从未生效过时 None
        """
        cur = await self._conn.execute(
            "SELECT * FROM research_prompt_versions "
            "WHERE md5=? AND status='applied' AND created_at <= ? ORDER BY id DESC LIMIT 1",
            (md5, as_of_ts),
        )
        row = await cur.fetchone()
        return ResearchPromptVersion(**dict(row)) if row else None

    async def latest_applied_by_md5(self, md5: str) -> ResearchPromptVersion | None:
        """按正文 md5 解析当前最新生效的版本（研报构建时点归因用，issue #113 R5-4）。

        与 get_version_by_md5 的区别：不带 as_of 时点——构建 prompt 的当下，
        "该 md5 最新 applied 版本"就是研报实际使用的版本，直接落库为
        research_reports.research_prompt_version_id，消除复盘侧 md5+时点反解的歧义。

        参数：
            md5: str，提示词正文摘要（构建 prompt 时点取样的 body_md5）

        返回：
            ResearchPromptVersion | None：该 md5 最新生效的版本；无 applied 记录时 None
        """
        cur = await self._conn.execute(
            "SELECT * FROM research_prompt_versions "
            "WHERE md5=? AND status='applied' ORDER BY id DESC LIMIT 1",
            (md5,),
        )
        row = await cur.fetchone()
        return ResearchPromptVersion(**dict(row)) if row else None

    async def set_version_status(self, version_id: int, status: str) -> None:
        """更新版本状态（draft→applied 生效、draft→discarded 废弃）。

        参数：
            version_id: int，版本编号
            status: str，目标状态（applied/discarded）

        返回：
            None，就地更新数据库并提交
        """
        await self._conn.execute(
            "UPDATE research_prompt_versions SET status=? WHERE id=?", (status, version_id)
        )
        await self._conn.commit()

    async def latest_applied_version(self) -> ResearchPromptVersion | None:
        """读取最新一个 applied 状态的版本；无则返回 None。

        供启动对账：文件 md5 与最新生效版本不一致时以数据库为准恢复文件。

        参数：无

        返回：
            ResearchPromptVersion | None：最新生效版本；版本表无 applied 记录时 None
        """
        cur = await self._conn.execute(
            "SELECT * FROM research_prompt_versions WHERE status='applied' ORDER BY id DESC LIMIT 1"
        )
        row = await cur.fetchone()
        return ResearchPromptVersion(**dict(row)) if row is not None else None

    async def discard_all_drafts(self) -> int:
        """把全部 draft 状态的版本置为 discarded（启动时清理孤儿草稿）。

        启动时不存在进行中的复盘轮——此刻仍为 draft 的版本必然是上轮异常残留，
        留在历史里可能被人工回滚激活为过期内容。

        参数：无

        返回：
            int：废弃的草稿数量
        """
        cur = await self._conn.execute(
            "UPDATE research_prompt_versions SET status='discarded' WHERE status='draft'"
        )
        await self._conn.commit()
        return cur.rowcount

    async def attach_report_to_version(self, version_id: int, review_report_id: int) -> None:
        """回填触发该版本的复盘报告 id（版本先落库、报告后落库的反向关联）。

        参数：
            version_id: int，版本编号
            review_report_id: int，复盘报告编号

        返回：
            None，就地更新数据库并提交
        """
        await self._conn.execute(
            "UPDATE research_prompt_versions SET review_report_id=? WHERE id=?",
            (review_report_id, version_id),
        )
        await self._conn.commit()
