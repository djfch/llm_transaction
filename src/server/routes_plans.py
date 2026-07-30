"""交易计划只读路由：GET /api/plans 分页查询（从 routes_status 独立成文件控制体量）。

计划由执行 agent 的 save_trade_plan / close_trade_plan 工具写入，
本路由只读展示（无写端点）；数据经 repo.plans 子仓库取用。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from src.server.deps import ServerDeps


def create_plans_router(deps: ServerDeps) -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.get("/plans")
    async def get_plans(
        status: str = Query("", pattern="^(active|executed|cancelled)?$"),
        offset: int = Query(0, ge=0),
        limit: int = Query(20, ge=1, le=200),
    ) -> dict[str, Any]:
        """分页返回交易计划（最新在前）；status 空串返回全部状态。"""
        plans, total = await deps.repo.plans.list_plans_page(
            limit=limit, offset=offset, status=status or None
        )
        return {
            "items": [p.model_dump() for p in plans],
            "total": total,
            "offset": offset,
            "limit": limit,
        }

    return router
