"""交易计划只读路由：GET /api/plan 返回当前唯一一份计划（从 routes_status 独立成文件控制体量）。

计划由执行 agent 的 update_trade_plan / clear_trade_plan 工具写入（全文覆盖更新），
本路由只读展示（无写端点）；数据经 repo.plans 子仓库取用。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from src.server.deps import ServerDeps


def create_plans_router(deps: ServerDeps) -> APIRouter:
    """创建交易计划只读路由：GET /api/plan 返回当前唯一一份计划。

    参数：
        deps: ServerDeps，服务依赖束（计划数据经其 repo.plans 子仓库读取）

    返回：
        APIRouter：挂载 /api/plan 只读端点的路由器
    """
    router = APIRouter(prefix="/api")

    @router.get("/plan")
    async def get_plan() -> dict[str, Any]:
        """当前交易计划；无计划时 content 为空串（前端据此显示空态）。

        参数：无
        返回：
            dict[str, Any]，当前交易计划；无计划时 content 为空串（前端据此显示空态）
        """
        plan = await deps.repo.plans.get_plan()
        if plan is None:
            return {"content": "", "round_id": "", "updated_at": None}
        return {
            "content": plan.content,
            "round_id": plan.round_id,
            "updated_at": plan.updated_at,
        }

    return router
