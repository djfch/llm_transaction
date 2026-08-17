"""研报端点：研报分页列表/详情（含因果链）、手动触发研报、研报实时状态。

写操作（手动触发研报）经 ServerDeps 回调注入，None 时诚实 503；
状态码映射走回调返回的结构化 error_code，不做错误文案子串匹配；
路由内不直接 SQL（取数经 deps.repo.research）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel, Field

from src.config import load_settings
from src.memory.models import CausalLink, ResearchAssetView, ResearchReport
from src.server.deps import ServerDeps
from src.server.routes_status import (  # tool_calls/JSON 字段与 /agent/live 同一序列化口径
    _parse_json_field,
    _tool_call_item,
)


class _ResearchRunBody(BaseModel):
    """POST /research/run 可选 body：报告类型与回看窗口（小时）；无 body = 默认 manual/24h。"""

    report_type: str = "manual"
    hours: int = Field(default=24, ge=1, le=48)


def _asset_summary(view: ResearchAssetView) -> dict[str, Any]:
    """把单标的结论视图压成摘要字典，只保留前端契约约定的 8 个字段。

    参数：
        view: ResearchAssetView，研报中单合约的结论视图（含当时市场输入快照）

    返回：
        dict[str, Any]：逐标的摘要，含 contract（合约名）、direction（方向）、
        confidence（置信度）、horizon（预测窗口）、market_regime（市场状态）、
        technical_confirmation（技术确认）、basis_type（基差类型）、
        data_status（数据状态）共 8 个前端契约键
    """
    keys = (
        "contract",
        "direction",
        "confidence",
        "horizon",
        "market_regime",
        "technical_confirmation",
        "basis_type",
        "data_status",
    )
    dumped = view.model_dump()
    return {key: dumped[key] for key in keys}


def _asset_detail(view: ResearchAssetView) -> dict[str, Any]:
    """在摘要基础上补齐证据、风险与研判叙述，得到单标的完整详情字典。

    参数：
        view: ResearchAssetView，研报中单合约的结论视图（含当时市场输入快照）

    返回：
        dict[str, Any]：逐标的详情；在摘要 8 键之上追加 evidence（证据列表）、
        risks（风险列表）、narrative（研判叙述）、verify_result（复盘验证结果）、
        created_at（创建时间戳）；evidence/risks 由 *_json 字段解析为对象
    """
    item = _asset_summary(view)
    item.update(
        {
            "evidence": _parse_json_field(view.evidence_json),
            "risks": _parse_json_field(view.risks_json),
            "narrative": view.narrative,
            "verify_result": view.verify_result,
            "created_at": view.created_at,
        }
    )
    return item


def _report_item(report: ResearchReport, views: list[ResearchAssetView]) -> dict[str, Any]:
    """报告头只暴露当前协议字段；逐标的列表使用摘要形状。

    参数：
        report: ResearchReport，研报或复盘报告记录
        views: list[ResearchAssetView]，报告对应的逐标的研判列表

    返回：
        dict[str, Any]，包含报告头与逐标的摘要的响应字典
    """
    return {
        "id": report.id,
        "report_type": report.report_type,
        "schema_version": report.schema_version,
        "summary": report.summary,
        "cross_market_view": report.cross_market_view,
        "global_risks": _parse_json_field(report.global_risks_json),
        "error": report.error,
        "round_id": report.round_id,
        "created_at": report.created_at,
        "asset_views": [_asset_summary(view) for view in views],
    }


def _report_detail(report: ResearchReport, views: list[ResearchAssetView]) -> dict[str, Any]:
    """组装研报详情响应体：报告头字段不变，逐标的由摘要换成完整详情。

    参数：
        report: ResearchReport，研报报告头（summary/cross_market_view 等）
        views: list[ResearchAssetView]，该研报的逐标的结论视图列表

    返回：
        dict[str, Any]：研报详情；结构同 _report_item，但 asset_views 为完整详情形状
    """
    item = _report_item(report, views)
    item["asset_views"] = [_asset_detail(view) for view in views]
    return item


def _causal_link_item(link: CausalLink) -> dict[str, Any]:
    """因果链响应项：chain/evidence 解析为对象（键集为前端契约，不含 *_json 原名）。

    参数：
        link: CausalLink，因果链记录

    返回：
        dict[str, Any]，chain 与 evidence 已解析为对象的因果链响应项
    """
    return {
        "id": link.id,
        "report_id": link.report_id,
        "chain": _parse_json_field(link.chain_json),
        "confidence": link.confidence,
        "evidence": _parse_json_field(link.evidence_json),
        "status": link.status,
        "broken_at": link.broken_at,
        "topic": link.topic,
        "supersedes_id": link.supersedes_id,
        "await_verification": link.await_verification,
        "created_at": link.created_at,
    }


def create_research_router(deps: ServerDeps) -> APIRouter:
    """装配研报路由（/api 前缀）：分页列表、详情、实时状态与手动触发四个端点。

    参数：
        deps: ServerDeps，服务端依赖集合；取数经 deps.repo.research，
        手动触发经 deps.research_run 回调（None 时该端点诚实 503）

    返回：
        APIRouter：挂载 /research/reports、/research/reports/{report_id}、
        /research/live、/research/run 四个端点的路由器
    """
    router = APIRouter(prefix="/api")

    @router.get("/research/schedule-status")
    async def get_research_schedule_status() -> dict[str, Any]:
        """读取研报自动调度总开关、下一次执行与官方日历状态。

        参数：无

        返回：
            dict[str, Any]：调度器生成的只读状态快照

        异常：
            HTTPException，研报调度状态未接线时返回 503
        """
        if deps.research_schedule_status is None:
            raise HTTPException(status_code=503, detail="研报自动调度状态未接线")
        return deps.research_schedule_status()

    @router.get("/research/reports")
    async def list_research_reports(
        offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=200)
    ) -> dict[str, Any]:
        """研报分页列表（最新在前，含失败记录与逐标的摘要）。

        参数：
            offset: int，分页偏移量
            limit: int，返回记录数量上限

        返回：
            dict[str, Any]，研报分页列表（最新在前，含失败记录与逐标的摘要）
        """
        reports, total = await deps.repo.research.list_reports_page(limit=limit, offset=offset)
        items = []
        for report in reports:
            views = await deps.repo.research.list_asset_views_by_report(report.id)
            items.append(_report_item(report, views))
        return {"items": items, "total": total}

    @router.get("/research/reports/{report_id}")
    async def get_research_report(report_id: int) -> dict[str, Any]:
        """研报详情：逐标的证据/风险/研判 + 该研报因果链（空为 []）。

        参数：
            report_id: int，报告编号

        返回：
            dict[str, Any]，研报详情：逐标的证据/风险/研判 + 该研报因果链（空为 []）

        异常：
            HTTPException，指定研报不存在时以 404 响应
        """
        report = await deps.repo.research.get_report(report_id)
        if report is None:
            raise HTTPException(status_code=404, detail=f"研报不存在: {report_id}")
        views = await deps.repo.research.list_asset_views_by_report(report_id)
        item = _report_detail(report, views)
        links = await deps.repo.research.list_causal_links_by_report(report_id)
        item["causal_links"] = [_causal_link_item(link) for link in links]
        return item

    @router.get("/research/live")
    async def get_research_live() -> dict[str, Any]:
        """实时研报展示：当前模式最新一轮研报审计（wake_source='research'）+ 已落库工具调用。

        响应键与前端契约冻结：round 键集同 /api/review/live（model_dump 不含 mode，
        进行中的轮 ended_at 为 null）、tool_calls 项同一形状（复用 _tool_call_item，
        args/result 为已解析对象）；mode 口径同 get_review_live（runtime_settings 优先，
        未接线回退配置文件）；无研报轮时 round 为 null、tool_calls 为空。

        参数：无

        返回：
            dict[str, Any]，实时研报展示：当前模式最新一轮研报审计（wake_source='research'）+ 已落库工具调用。  响应键与前端契约冻结：round 键集同 /api/review/live（model_dump 不含 mode， 进行中的轮 ended_at 为 null）、tool_calls 项同一形状（复用 _tool_call_item， args/result 为已解析对象）；mode 口径同 get_review_live（runtime_settings 优先， 未接线回退配置文件）；无研报轮时 round 为 null、tool_calls 为空

        """
        settings = deps.runtime_settings or load_settings(deps.config_path)
        round_row = await deps.repo.research.latest_research_audit_round(settings.mode)
        if round_row is None:
            return {"round": None, "tool_calls": []}
        calls = await deps.repo.list_audit_tool_calls(round_row.round_id)
        return {
            "round": round_row.model_dump(exclude={"mode"}),  # 契约不含 mode 键（同 /review/live）
            "tool_calls": [_tool_call_item(c) for c in calls],
        }

    @router.post("/research/run")
    async def run_research_now(body: _ResearchRunBody | None = Body(None)) -> dict[str, Any]:
        """手动触发研报（点火即返回）：无 body 用调度默认值（manual/24h）；有 body 按指定类型与窗口透传。

        点火成功立即返回 started/report_type/hours，不等待生成完成；生成进度与结果经
        WS 事件、/research/live 轮询与报告列表呈现（失败报告照常落库入列）。
        回调未接线 503；LLM 未配置 503；研报进行中 409；hours 越界 422（pydantic）。
        状态码映射走回调返回的结构化 error_code（llm_not_configured/busy）。

        参数：
            body: _ResearchRunBody | None，可选的手动触发请求体

        返回：
            dict[str, Any]：点火结果（started、report_type、hours 回显），不含执行结果

        异常：
            HTTPException，研报未接线或 LLM 未配置时以 503 响应，调度锁占用时以 409 响应
        """
        if deps.research_run is None:
            raise HTTPException(status_code=503, detail="研报未接线（agent 未装配研报调度）")
        if body is None:
            result = await deps.research_run()
        else:
            result = await deps.research_run(report_type=body.report_type, hours=body.hours)
        error_code = result.get("error_code")
        if error_code == "llm_not_configured":
            raise HTTPException(status_code=503, detail=result.get("error", ""))
        if error_code == "busy":
            raise HTTPException(status_code=409, detail=result.get("error", ""))
        return result

    return router
