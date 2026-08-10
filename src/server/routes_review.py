"""复盘与策略版本端点：复盘报告只读/手动触发、复盘实时状态、策略版本列表/详情/diff/回滚。

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

from src.config import load_settings
from src.memory.models import ReviewReport, StrategyVersion
from src.review.strategy import StrategyValidationError
from src.server.deps import ServerDeps
from src.server.routes_status import _tool_call_item  # tool_calls 与 /agent/live 同一序列化口径

_LIST_REPORT_MD_LIMIT = 200  # 列表项 report_md 截断长度（省流量，详情端点给全文）


class _ReviewRunBody(BaseModel):
    """POST /review/run 可选 body：人工补跑的历史区间（Unix 秒）；无 body = 最近 interval_days 天区间。"""

    start_ts: float
    end_ts: float


def _report_item(report: ReviewReport, *, truncate: bool) -> dict[str, Any]:
    """报告响应项：10 个契约键固定；truncate 时 report_md 截断（列表省流量，键名不变）。

    参数：
        report: ReviewReport，研报或复盘报告记录
        truncate: bool，是否截断报告正文

    返回：
        dict[str, Any]，契约键固定的复盘报告响应字典
    """
    item = report.model_dump()
    if truncate:
        item["report_md"] = item["report_md"][:_LIST_REPORT_MD_LIMIT]
    return item


def _version_item(version: StrategyVersion) -> dict[str, Any]:
    """版本列表项：不含 content（省流量）；全文走 /strategy/versions/{id}。

    参数：
        version: StrategyVersion，策略版本记录

    返回：
        dict[str, Any]，不含策略全文的版本列表项
    """
    return version.model_dump(exclude={"content"})


async def _get_version_or_404(deps: ServerDeps, version_id: int) -> StrategyVersion:
    """按 id 读取策略版本，供详情与 diff 端点共用；查不到时按 404 处理。

    参数：
        deps: ServerDeps，服务端依赖容器（取数经 deps.repo）
        version_id: int，策略版本 id

    返回：
        StrategyVersion：该 id 对应的策略版本记录

    异常：
        HTTPException(404)：版本不存在时抛出
    """
    version = await deps.repo.review.get_strategy_version(version_id)
    if version is None:
        raise HTTPException(status_code=404, detail=f"策略版本不存在: {version_id}")
    return version


def create_review_router(deps: ServerDeps) -> APIRouter:
    """装配复盘与策略版本相关端点（前缀 /api），写操作全部经 deps 回调注入。

    参数：
        deps: ServerDeps，服务端依赖容器；写操作回调（复盘触发、策略回滚）未接线时端点诚实 503

    返回：
        APIRouter：已注册复盘报告/实时状态/手动复盘、策略版本列表/详情/diff/回滚端点的路由器
    """
    router = APIRouter(prefix="/api")

    @router.get("/review/reports")
    async def list_review_reports(
        offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=200)
    ) -> dict[str, Any]:
        """复盘报告分页列表（最新在前）；report_md 截断 200 字符省流量。

        参数：
            offset: int，分页偏移量
            limit: int，返回记录数量上限

        返回：
            dict[str, Any]，复盘报告分页列表（最新在前）；report_md 截断 200 字符省流量
        """
        reports, total = await deps.repo.review.list_review_reports_page(limit=limit, offset=offset)
        return {"items": [_report_item(r, truncate=True) for r in reports], "total": total}

    @router.get("/review/reports/{report_id}")
    async def get_review_report(report_id: int) -> dict[str, Any]:
        """复盘报告详情：report_md 全文（与列表项同一组契约键）。

        参数：
            report_id: int，报告编号

        返回：
            dict[str, Any]，复盘报告详情：report_md 全文（与列表项同一组契约键）

        异常：
            HTTPException，指定复盘报告不存在时以 404 响应
        """
        report = await deps.repo.review.get_review_report(report_id)
        if report is None:
            raise HTTPException(status_code=404, detail=f"复盘报告不存在: {report_id}")
        return _report_item(report, truncate=False)

    @router.get("/review/live")
    async def get_review_live() -> dict[str, Any]:
        """实时复盘展示：当前模式最新一轮复盘审计（wake_source='review'）+ 已落库工具调用。

        响应键与前端契约冻结：round 键集同 /api/agent/live（model_dump 不含 mode，
        进行中的轮 ended_at 为 null）、tool_calls 项同一形状（复用 _tool_call_item，
        args/result 为已解析对象）；mode 口径同 get_agent_live（runtime_settings 优先，
        未接线回退配置文件）；无复盘轮时 round 为 null、tool_calls 为空。

        参数：无

        返回：
            dict[str, Any]，实时复盘展示：当前模式最新一轮复盘审计（wake_source='review'）+ 已落库工具调用。  响应键与前端契约冻结：round 键集同 /api/agent/live（model_dump 不含 mode， 进行中的轮 ended_at 为 null）、tool_calls 项同一形状（复用 _tool_call_item， args/result 为已解析对象）；mode 口径同 get_agent_live（runtime_settings 优先， 未接线回退配置文件）；无复盘轮时 round 为 null、tool_calls 为空

        """
        settings = deps.runtime_settings or load_settings(deps.config_path)
        round_row = await deps.repo.review.latest_review_audit_round(settings.mode)
        if round_row is None:
            return {"round": None, "tool_calls": []}
        calls = await deps.repo.list_audit_tool_calls(round_row.round_id)
        return {
            "round": round_row.model_dump(exclude={"mode"}),  # 契约不含 mode 键（同 /agent/live）
            "tool_calls": [_tool_call_item(c) for c in calls],
        }

    @router.post("/review/run")
    async def run_review_now(body: _ReviewRunBody | None = Body(None)) -> dict[str, Any]:
        """手动触发复盘：无 body 维持最近 interval_days 天区间；有 body（人工补跑）按指定区间透传。

        回调未接线 503；LLM 未配置 503；复盘进行中 409；区间非法 422。
        状态码映射走回调返回的结构化 error_code（llm_not_configured/busy/invalid_period），
        不做错误文案子串匹配。

        参数：
            body: _ReviewRunBody | None，可选的手动触发请求体

        返回：
            dict[str, Any]，手动触发复盘：无 body 维持最近 interval_days 天区间；有 body（人工补跑）按指定区间透传。  回调未接线 503；LLM 未配置 503；复盘进行中 409；区间非法 422。 状态码映射走回调返回的结构化 error_code（llm_not_configured/busy/invalid_period）， 不做错误文案子串匹配

        异常：
            HTTPException，复盘未接线或 LLM 未配置时以 503 响应；调度锁占用时以 409 响应；补跑区间非法时以 422 响应

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
        """策略版本列表（最新在前）：不含 content，省流量。

        参数：无

        返回：
            dict[str, Any]，策略版本列表（最新在前）：不含 content，省流量
        """
        versions = await deps.repo.review.list_strategy_versions()
        return {"items": [_version_item(v) for v in versions]}

    @router.get("/strategy/versions/{version_id}")
    async def get_strategy_version(version_id: int) -> dict[str, Any]:
        """策略版本详情：含 content 全文。

        参数：
            version_id: int，策略版本编号

        返回：
            dict[str, Any]，策略版本详情：含 content 全文
        """
        return (await _get_version_or_404(deps, version_id)).model_dump()

    @router.get("/strategy/diff", response_class=PlainTextResponse)
    async def diff_strategy_versions(from_id: int = Query(alias="from"), to: int = Query()) -> str:
        """两版本策略书 unified diff（纯文本）；参数非法 422、版本不存在 404。

        参数：
            from_id: int，起始策略版本编号
            to: int，目标策略版本编号

        返回：
            str，两版本策略书 unified diff（纯文本）；参数非法 422、版本不存在 404
        """
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
        """回滚到指定策略版本：回调未接线 503；版本不存在 404。

        参数：
            version_id: int，策略版本编号

        返回：
            dict[str, Any]，回滚到指定策略版本：回调未接线 503；版本不存在 404

        异常：
            HTTPException，策略版本管理未接线时以 503 响应，目标版本不存在时以 404 响应
        """
        if deps.strategy_rollback is None:
            raise HTTPException(status_code=503, detail="策略版本管理未接线（agent 未装配）")
        try:
            return await deps.strategy_rollback(version_id)
        except StrategyValidationError as exc:  # 回滚的唯一校验失败即版本不存在
            raise HTTPException(status_code=404, detail="；".join(exc.reasons)) from exc

    return router
