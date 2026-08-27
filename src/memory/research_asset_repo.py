"""研报 v2 逐标的结论存取 mixin：报告头与资产结论原子落库。"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Protocol

import aiosqlite

from src.memory.models import ResearchAssetView, ResearchReport


class _HasConnection(Protocol):
    @property
    def _db_path(self) -> Path:
        """提供 SQLite 数据库文件路径，供独立开连接的写操作使用。

        参数：无

        返回：
            Path：SQLite 数据库文件路径
        """
        ...

    @property
    def _conn(self) -> aiosqlite.Connection:
        """提供共享的 SQLite 连接，供只读查询复用。

        参数：无

        返回：
            aiosqlite.Connection：宿主类持有的共享数据库连接
        """
        ...


class ResearchAssetRepoMixin:
    """由 ResearchRepo 继承；只依赖其共享连接属性。"""

    async def save_report_bundle(
        self: _HasConnection,
        *,
        report_type: str,
        summary: str,
        cross_market_view: str,
        global_risks_json: str,
        raw_json: str,
        round_id: str,
        asset_views: list[dict],
        research_prompt_md5: str = "",
    ) -> tuple[ResearchReport, list[ResearchAssetView]]:
        """原子保存当前报告头与全部逐标的结论。

        参数：
            report_type: str，研报类型
            summary: str，研报摘要
            cross_market_view: str，跨市场观点
            global_risks_json: str，序列化后的全局风险列表
            raw_json: str，模型原始输出 JSON
            round_id: str，研报轮次标识
            asset_views: list[dict]，全部逐标的结论数据
            research_prompt_md5: str，生成本研报所用的 research_prompt.md 正文 md5
                （与 research_prompt_versions.md5 关联；缺省空串表示未接线）
        返回：
            tuple[ResearchReport, list[ResearchAssetView]]，原子保存当前报告头与全部逐标的结论
        异常：
            ValueError，成功研报未包含任何逐标的结论时抛出
            Exception，事务写入失败时回滚并原样重新抛出
        """
        if not asset_views:
            raise ValueError("成功报告至少包含一个逐标的结论")
        created_at = time.time()
        conn = await aiosqlite.connect(str(self._db_path))
        try:
            await conn.execute("BEGIN IMMEDIATE")
            cur = await conn.execute(
                "INSERT INTO research_reports(report_type,schema_version,summary,"
                "cross_market_view,global_risks_json,raw_json,error,round_id,created_at,"
                "research_prompt_md5)"
                " VALUES(?,3,?,?,?,?,?,?,?,?)",
                (
                    report_type,
                    summary,
                    cross_market_view,
                    global_risks_json,
                    raw_json,
                    "",
                    round_id,
                    created_at,
                    research_prompt_md5,
                ),
            )
            report_id = cur.lastrowid or 0
            views = await self._insert_asset_views(conn, report_id, asset_views, created_at)
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise
        finally:
            await conn.close()
        report = ResearchReport(
            id=report_id,
            report_type=report_type,
            schema_version=3,
            summary=summary,
            cross_market_view=cross_market_view,
            global_risks_json=global_risks_json,
            raw_json=raw_json,
            round_id=round_id,
            created_at=created_at,
            research_prompt_md5=research_prompt_md5,
        )
        return report, views

    async def _insert_asset_views(
        self: _HasConnection,
        conn: aiosqlite.Connection,
        report_id: int,
        items: list[dict],
        created_at: float,
    ) -> list[ResearchAssetView]:
        """在给定连接的事务内逐条写入某次研报的全部逐标的结论。

        参数：
            conn: aiosqlite.Connection，调用方已开启事务的数据库连接，本函数不提交不回滚
            report_id: int，所属研报报告头的 id
            items: list[dict]，逐标的结论字典列表，键对应 research_asset_views 表字段
            created_at: float，写入时间戳（Unix 秒），与报告头保持一致

        返回：
            list[ResearchAssetView]：已写入的逐标的结论列表，id 为数据库自增主键
        """
        views: list[ResearchAssetView] = []
        for item in items:
            values = {**item, "report_id": report_id, "created_at": created_at}
            cur = await conn.execute(
                "INSERT INTO research_asset_views(report_id,contract,direction,confidence,horizon,"
                "market_regime,technical_confirmation,basis_type,data_status,evidence_json,"
                "risks_json,narrative,market_context_json,created_at)"
                " VALUES(:report_id,:contract,:direction,:confidence,:horizon,:market_regime,"
                ":technical_confirmation,:basis_type,:data_status,:evidence_json,:risks_json,"
                ":narrative,:market_context_json,:created_at)",
                values,
            )
            views.append(ResearchAssetView(id=cur.lastrowid or 0, **values))
        return views

    async def list_asset_views_by_report(
        self: _HasConnection, report_id: int
    ) -> list[ResearchAssetView]:
        """按报告 id 读取该次研报的全部逐标的结论，按写入顺序排列。

        参数：
            report_id: int，研报报告头的 id

        返回：
            list[ResearchAssetView]：该报告下全部逐标的结论；无记录时返回空列表
        """
        cur = await self._conn.execute(
            "SELECT * FROM research_asset_views WHERE report_id=? ORDER BY id", (report_id,)
        )
        return [ResearchAssetView(**dict(row)) for row in await cur.fetchall()]

    async def latest_asset_view(self: _HasConnection, contract: str) -> ResearchAssetView | None:
        """读取指定合约最近一次成功研报中的逐标的结论。

        参数：
            contract: str，合约名（如 BTC_USDT）

        返回：
            ResearchAssetView | None：最新一次成功研报（error 为空）中该合约的结论；
            从未有过成功结论时返回 None
        """
        cur = await self._conn.execute(
            "SELECT view.* FROM research_asset_views view "
            "JOIN research_reports report ON report.id=view.report_id "
            "WHERE view.contract=? AND report.error='' "
            "ORDER BY report.id DESC LIMIT 1",
            (contract,),
        )
        row = await cur.fetchone()
        return ResearchAssetView(**dict(row)) if row else None
