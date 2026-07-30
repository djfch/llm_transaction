"""复盘与策略版本端点：复盘报告只读/手动触发、策略版本列表/详情/diff/回滚。

写操作（手动复盘、策略回滚）全部经 ServerDeps 回调注入，None 时诚实 503；
从 src.review.strategy 仅 import StrategyValidationError 异常契约做错误映射，
行为不依赖复盘具体实现；路由内不直接 SQL（取数经 deps.repo）。
"""

from __future__ import annotations

import difflib
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from src.memory.models import ReviewReport, StrategyVersion
from src.review.strategy import StrategyValidationError
from src.server.deps import ServerDeps

_LIST_REPORT_MD_LIMIT = 200  # 列表项 report_md 截断长度（省流量，详情端点给全文）


class _ReviewRunBody(BaseModel):
    """POST /review/run 可选 body：人工补跑的历史区间（Unix 秒）；无 body = 最近 interval_days 天区间。"""

    start_ts: float
    end_ts: float


def _report_item(report: ReviewReport, *, truncate: bool) -> dict[str, Any]:
    """报告响应项：9 个契约键固定；truncate 时 report_md 截断（列表省流量，键名不变）。"""
    item = report.model_dump()
    if truncate:
        item["report_md"] = item["report_md"][:_LIST_REPORT_MD_LIMIT]
    return item


def _version_item(version: StrategyVersion) -> dict[str, Any]:
    """版本列表项：不含 content（省流量）；全文走 /strategy/versions/{id}。"""
    return version.model_dump(exclude={"content"})


async def _get_version_or_404(deps: ServerDeps, version_id: int) -> StrategyVersion:
    version = await deps.repo.review.get_strategy_version(version_id)
    if version is None:
        raise HTTPException(status_code=404, detail=f"策略版本不存在: {version_id}")
    return version


def create_review_router(deps: ServerDeps) -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.get("/review/reports")
    async def list_review_reports(
        offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=200)
    ) -> dict[str, Any]:
        """复盘报告分页列表（最新在前）；report_md 截断 200 字符省流量。"""
        reports, total = await deps.repo.review.list_review_reports_page(limit=limit, offset=offset)
        return {"items": [_report_item(r, truncate=True) for r in reports], "total": total}

    @router.get("/review/reports/{report_id}")
    async def get_review_report(report_id: int) -> dict[str, Any]:
        """复盘报告详情：report_md 全文（与列表项同一组契约键）。"""
        report = await deps.repo.review.get_review_report(report_id)
        if report is None:
            raise HTTPException(status_code=404, detail=f"复盘报告不存在: {report_id}")
        return _report_item(report, truncate=False)

    @router.post("/review/run")
    async def run_review_now(body: _ReviewRunBody | None = Body(None)) -> dict[str, Any]:
        """手动触发复盘：无 body 维持最近 interval_days 天区间；有 body（人工补跑）按指定区间透传。

        回调未接线 503；LLM 未配置 503；复盘进行中 409；区间非法 422。
        状态码映射走回调返回的结构化 error_code（llm_not_configured/busy/invalid_period），
        不做错误文案子串匹配。
        """
        if deps.review_run is None:
            raise HTTPException(status_code=503, detail="复盘未接线（agent 未装配复盘调度）")
        if body is None:
            result = await deps.review_run()
        else:
            result = await deps.review_run(period_start=body.start_ts, period_end=body.end_ts)
        error_code = result.get("error_code")
        if error_code == "llm_not_configured":
            raise HTTPException(status_code=503, detail=result.get("error", ""))
        if error_code == "busy":
            raise HTTPException(status_code=409, detail=result.get("error", ""))
        if error_code == "invalid_period":
            raise HTTPException(status_code=422, detail=result.get("error", ""))
        return result

    @router.get("/strategy/versions")
    async def list_strategy_versions() -> dict[str, Any]:
        """策略版本列表（最新在前）：不含 content，省流量。"""
        versions = await deps.repo.review.list_strategy_versions()
        return {"items": [_version_item(v) for v in versions]}

    @router.get("/strategy/versions/{version_id}")
    async def get_strategy_version(version_id: int) -> dict[str, Any]:
        """策略版本详情：含 content 全文。"""
        return (await _get_version_or_404(deps, version_id)).model_dump()

    @router.get("/strategy/diff", response_class=PlainTextResponse)
    async def diff_strategy_versions(from_id: int = Query(alias="from"), to: int = Query()) -> str:
        """两版本策略书 unified diff（纯文本）；参数非法 422、版本不存在 404。"""
        from_version = await _get_version_or_404(deps, from_id)
        to_version = await _get_version_or_404(deps, to)
        return "\n".join(
            difflib.unified_diff(
                from_version.content.splitlines(),
                to_version.content.splitlines(),
                fromfile=f"v{from_id}",
                tofile=f"v{to}",
                lineterm="",
            )
        )

    @router.post("/strategy/rollback/{version_id}")
    async def rollback_strategy(version_id: int) -> dict[str, Any]:
        """回滚到指定策略版本：回调未接线 503；版本不存在 404。"""
        if deps.strategy_rollback is None:
            raise HTTPException(status_code=503, detail="策略版本管理未接线（agent 未装配）")
        try:
            return await deps.strategy_rollback(version_id)
        except StrategyValidationError as exc:  # 回滚的唯一校验失败即版本不存在
            raise HTTPException(status_code=404, detail="；".join(exc.reasons)) from exc

    return router
