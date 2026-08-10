"""研报 v2 逐标的结论存取 mixin：报告头与资产结论原子落库。"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Protocol

import aiosqlite

from src.memory.models import ResearchAssetView, ResearchReport


class _HasConnection(Protocol):
    @property
    def _db_path(self) -> Path: ...

    @property
    def _conn(self) -> aiosqlite.Connection: ...


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
    ) -> tuple[ResearchReport, list[ResearchAssetView]]:
        """原子保存当前报告头与全部逐标的结论。"""
        if not asset_views:
            raise ValueError("成功报告至少包含一个逐标的结论")
        created_at = time.time()
        conn = await aiosqlite.connect(str(self._db_path))
        try:
            await conn.execute("BEGIN IMMEDIATE")
            cur = await conn.execute(
                "INSERT INTO research_reports(report_type,schema_version,summary,"
                "cross_market_view,global_risks_json,raw_json,error,round_id,created_at)"
                " VALUES(?,2,?,?,?,?,?,?,?)",
                (
                    report_type,
                    summary,
                    cross_market_view,
                    global_risks_json,
                    raw_json,
                    "",
                    round_id,
                    created_at,
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
            schema_version=2,
            summary=summary,
            cross_market_view=cross_market_view,
            global_risks_json=global_risks_json,
            raw_json=raw_json,
            round_id=round_id,
            created_at=created_at,
        )
        return report, views

    async def _insert_asset_views(
        self: _HasConnection,
        conn: aiosqlite.Connection,
        report_id: int,
        items: list[dict],
        created_at: float,
    ) -> list[ResearchAssetView]:
        views: list[ResearchAssetView] = []
        for item in items:
            values = {**item, "report_id": report_id, "created_at": created_at}
            cur = await conn.execute(
                "INSERT INTO research_asset_views(report_id,contract,direction,confidence,horizon,"
                "market_regime,technical_confirmation,basis_type,data_status,evidence_json,"
                "risks_json,narrative,market_context_json,verify_result,created_at)"
                " VALUES(:report_id,:contract,:direction,:confidence,:horizon,:market_regime,"
                ":technical_confirmation,:basis_type,:data_status,:evidence_json,:risks_json,"
                ":narrative,:market_context_json,'',:created_at)",
                values,
            )
            views.append(ResearchAssetView(id=cur.lastrowid or 0, verify_result="", **values))
        return views

    async def list_asset_views_by_report(
        self: _HasConnection, report_id: int
    ) -> list[ResearchAssetView]:
        cur = await self._conn.execute(
            "SELECT * FROM research_asset_views WHERE report_id=? ORDER BY id", (report_id,)
        )
        return [ResearchAssetView(**dict(row)) for row in await cur.fetchall()]

    async def latest_asset_view(self: _HasConnection, contract: str) -> ResearchAssetView | None:
        cur = await self._conn.execute(
            "SELECT view.* FROM research_asset_views view "
            "JOIN research_reports report ON report.id=view.report_id "
            "WHERE view.contract=? AND report.error='' "
            "ORDER BY report.id DESC LIMIT 1",
            (contract,),
        )
        row = await cur.fetchone()
        return ResearchAssetView(**dict(row)) if row else None
