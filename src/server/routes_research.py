"""研报端点：研报分页列表/详情（含因果链与研报复盘记录）、手动触发研报、研报实时状态、
研报提示词在线编辑与版本历史（列表/详情/diff/回滚，issue #113）。

写操作（手动触发研报、研报提示词保存/回滚）经 ServerDeps 回调注入，None 时诚实 503
（提示词 PUT 例外：未接线时直写提示词文件，与 /strategy 未接线口径一致）；
状态码映射走回调返回的结构化 error_code，不做错误文案子串匹配；
从 src.research.prompt_store 仅 import ResearchPromptValidationError 异常契约做错误映射；
路由内不直接 SQL（取数经 deps.repo.research / deps.repo.research_review /
deps.repo.research_prompt）。
"""

from __future__ import annotations

import difflib
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from src.config import load_settings
from src.memory.models import (
    AuditRound,
    CausalLink,
    ResearchAssetView,
    ResearchPromptVersion,
    ResearchReport,
    ResearchReview,
)
from src.research.prompt_store import ResearchPromptValidationError
from src.server.deps import ServerDeps
from src.server.routes_status import (  # tool_calls/JSON 字段与 /agent/live 同一序列化口径
    _llm_identity_fields,
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
        risks（风险列表）、narrative（研判叙述）、created_at（创建时间戳）；
        evidence/risks 由 *_json 字段解析为对象
    """
    item = _asset_summary(view)
    item.update(
        {
            "evidence": _parse_json_field(view.evidence_json),
            "risks": _parse_json_field(view.risks_json),
            "narrative": view.narrative,
            "created_at": view.created_at,
        }
    )
    return item


def _report_item(
    report: ResearchReport, views: list[ResearchAssetView], audit: AuditRound | None = None
) -> dict[str, Any]:
    """报告头只暴露当前协议字段（含模型身份四键与研报提示词 md5 归因）；逐标的列表使用摘要形状。

    参数：
        report: ResearchReport，研报或复盘报告记录
        views: list[ResearchAssetView]，报告对应的逐标的研判列表
        audit: AuditRound | None，报告关联的审计轮（取模型身份）；None 时身份四键全空串

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
        "research_prompt_md5": report.research_prompt_md5,
        "created_at": report.created_at,
        **_llm_identity_fields(audit),
        "asset_views": [_asset_summary(view) for view in views],
    }


def _report_detail(
    report: ResearchReport, views: list[ResearchAssetView], audit: AuditRound | None = None
) -> dict[str, Any]:
    """组装研报详情响应体：报告头字段不变，逐标的由摘要换成完整详情。

    参数：
        report: ResearchReport，研报报告头（summary/cross_market_view 等）
        views: list[ResearchAssetView]，该研报的逐标的结论视图列表
        audit: AuditRound | None，报告关联的审计轮（取模型身份）；None 时身份四键全空串

    返回：
        dict[str, Any]：研报详情；结构同 _report_item，但 asset_views 为完整详情形状
    """
    item = _report_item(report, views, audit)
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
        "topic": link.topic,
        "supersedes_id": link.supersedes_id,
        "created_at": link.created_at,
    }


def _research_review_item(review: ResearchReview) -> dict[str, Any]:
    """研报复盘响应项：evidence_reviews/outcome 解析为对象（键集为前端契约，不含 *_json 原名）。

    参数：
        review: ResearchReview，研报复盘记录

    返回：
        dict[str, Any]：复盘记录响应项；evidence_reviews（逐条依据评价）与
        outcome（客观行情结果）已由 *_json 字段解析为对象，不含 contract/report_id
        （挂在对应逐标的详情键下，归属已由分组表达）；review_kind（复盘种类：
        auto 自动 / manual 人工授权重评）与 rereview_reason（授权理由，自动复盘
        为空串）为 R5-2 新增契约键
    """
    return {
        "id": review.id,
        "review_report_id": review.review_report_id,
        "direction_relation": review.direction_relation,
        "direction_reason": review.direction_reason,
        "reasoning_quality": review.reasoning_quality,
        "reasoning_review": review.reasoning_review,
        "evidence_reviews": _parse_json_field(review.evidence_reviews_json),
        "confidence_assessment": review.confidence_assessment,
        "confidence_reason": review.confidence_reason,
        "improvement_advice": review.improvement_advice,
        "outcome": _parse_json_field(review.outcome_json),
        "created_at": review.created_at,
        "review_kind": review.review_kind,
        "rereview_reason": review.rereview_reason,
    }


def _research_prompt_version_item(version: ResearchPromptVersion) -> dict[str, Any]:
    """研报提示词版本列表项：不含 content（省流量）；全文走 /research/prompt/versions/{id}。

    参数：
        version: ResearchPromptVersion，研报提示词版本记录

    返回：
        dict[str, Any]，不含提示词全文的版本列表项
    """
    return version.model_dump(exclude={"content"})


async def _get_research_prompt_version_or_404(
    deps: ServerDeps, version_id: int
) -> ResearchPromptVersion:
    """按 id 读取研报提示词版本，供详情与 diff 端点共用；查不到时按 404 处理。

    参数：
        deps: ServerDeps，服务端依赖容器（取数经 deps.repo）
        version_id: int，研报提示词版本 id

    返回：
        ResearchPromptVersion：该 id 对应的版本记录

    异常：
        HTTPException(404)：版本不存在时抛出
    """
    version = await deps.repo.research_prompt.get_version(version_id)
    if version is None:
        raise HTTPException(status_code=404, detail=f"研报提示词版本不存在: {version_id}")
    return version


def create_research_router(deps: ServerDeps) -> APIRouter:
    """装配研报路由（/api 前缀）：研报列表/详情/实时状态/手动触发 + 研报提示词版本族端点。

    参数：
        deps: ServerDeps，服务端依赖集合；取数经 deps.repo.research /
        deps.repo.research_review / deps.repo.research_prompt，
        手动触发经 deps.research_run 回调、提示词保存/回滚经
        deps.research_prompt_save / deps.research_prompt_rollback 回调
        （None 时对应端点诚实 503；提示词 PUT 例外，未接线时直写文件）

    返回：
        APIRouter：挂载 /research/reports、/research/reports/{report_id}、
        /research/live、/research/run、/research/prompt（GET/PUT）、
        /research/prompt/versions(+{id})、/research/prompt/diff、
        /research/prompt/rollback/{id} 端点的路由器
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
        # 批量取关联审计轮（免 N+1）：模型身份四键随报告返回，供跨模型对比
        audits = await deps.repo.list_audit_rounds([r.round_id for r in reports if r.round_id])
        items = []
        for report in reports:
            views = await deps.repo.research.list_asset_views_by_report(report.id)
            items.append(_report_item(report, views, audits.get(report.round_id)))
        return {"items": items, "total": total}

    @router.get("/research/reports/{report_id}")
    async def get_research_report(report_id: int) -> dict[str, Any]:
        """研报详情：逐标的证据/风险/研判 + 该研报因果链与研报复盘记录（空均为 []）。

        复盘记录按 contract 分组挂到每个 asset_view 详情的 research_reviews 键
        （同一研报可被多次复盘，故为列表；未被复盘的标的给空数组）。

        参数：
            report_id: int，报告编号

        返回：
            dict[str, Any]，研报详情：逐标的证据/风险/研判/复盘记录 + 该研报因果链（空为 []）

        异常：
            HTTPException，指定研报不存在时以 404 响应
        """
        report = await deps.repo.research.get_report(report_id)
        if report is None:
            raise HTTPException(status_code=404, detail=f"研报不存在: {report_id}")
        views = await deps.repo.research.list_asset_views_by_report(report_id)
        audit = await deps.repo.get_audit_round(report.round_id) if report.round_id else None
        item = _report_detail(report, views, audit)
        links = await deps.repo.research.list_causal_links_by_report(report_id)
        item["causal_links"] = [_causal_link_item(link) for link in links]
        reviews = await deps.repo.research_review.list_reviews_by_report(report_id)
        reviews_by_contract: dict[str, list[dict[str, Any]]] = {}
        for review in reviews:
            reviews_by_contract.setdefault(review.contract, []).append(
                _research_review_item(review)
            )
        for view_item in item["asset_views"]:
            view_item["research_reviews"] = reviews_by_contract.get(view_item["contract"], [])
        return item

    @router.get("/research/live")
    async def get_research_live(round_id: str | None = Query(default=None)) -> dict[str, Any]:
        """实时研报展示：默认当前模式最新一轮研报审计（wake_source='research'）+ 已落库工具调用。

        可选 ?round_id= 按 ID 直查（前端 pinned 轮询）：无 mode 过滤；查无此轮或命中
        异类轮（wake_source 非 research）按空态返回（HTTP 仍 200，供前端持续轮询）。

        响应键与前端契约冻结：round 键集同 /api/review/live（model_dump 不含 mode，
        进行中的轮 ended_at 为 null）、tool_calls 项同一形状（复用 _tool_call_item，
        args/result 为已解析对象）；mode 口径同 get_review_live（runtime_settings 优先，
        未接线回退配置文件）；无研报轮时 round 为 null、tool_calls 为空。

        参数：
            round_id: str | None，可选的审计轮次编号；提供时按 ID 直查替代最新轮口径

        返回：
            dict[str, Any]，实时研报展示：默认当前模式最新一轮研报审计（wake_source='research'）+ 已落库工具调用。
            可选 ?round_id= 按 ID 直查（前端 pinned 轮询）：无 mode 过滤；查无此轮或命中
            异类轮（wake_source 非 research）按空态返回（HTTP 仍 200，供前端持续轮询）。
            响应键与前端契约冻结：round 键集同 /api/review/live（model_dump 不含 mode，
            进行中的轮 ended_at 为 null）、tool_calls 项同一形状（复用 _tool_call_item，
            args/result 为已解析对象）；mode 口径同 get_review_live（runtime_settings 优先，
            未接线回退配置文件）；无研报轮时 round 为 null、tool_calls 为空

        """
        if round_id is None:
            settings = deps.runtime_settings or load_settings(deps.config_path)
            round_row = await deps.repo.research.latest_research_audit_round(settings.mode)
        else:
            round_row = await deps.repo.get_audit_round(round_id)
            if round_row is not None and round_row.wake_source != "research":
                round_row = None  # 异类轮（复盘/交易）按查无处理，不跨台返回
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

        点火成功立即返回 started/report_type/hours/round_id（预分配的审计轮次编号，
        与 WS research_round_start 事件同一身份，前端据此认轮），不等待生成完成；
        生成进度与结果经 WS 事件、/research/live 轮询与报告列表呈现（失败报告照常落库入列）。
        回调未接线 503；LLM 未配置 503；研报进行中 409；hours 越界 422（pydantic）。
        状态码映射走回调返回的结构化 error_code（llm_not_configured/busy）。

        参数：
            body: _ResearchRunBody | None，可选的手动触发请求体

        返回：
            dict[str, Any]：点火结果（started、round_id、report_type、hours 回显），不含执行结果

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

    @router.get("/research/prompt", response_class=PlainTextResponse)
    async def get_research_prompt() -> str:
        """研报提示词按纯文本（text/plain）返回，与前端约定一致（同 /strategy 口径）。

        参数：无

        返回：
            str：research_prompt.md 原文；文件不存在时返回空串
        """
        if not deps.research_prompt_path.exists():
            return ""
        return deps.research_prompt_path.read_text(encoding="utf-8")

    @router.put("/research/prompt", response_class=PlainTextResponse)
    async def put_research_prompt(request: Request) -> str:
        """保存研报提示词：接线后经 deps.research_prompt_save 走 ResearchPromptStore
        （校验 + 版本落库 + 原子生效），校验失败映 422（detail 为全部未过原因）；
        响应契约保持 PlainText 原文不变。未接线（测试 fake deps）时直接写入提示词文件。

        参数：
            request: Request，FastAPI 原始请求对象（body 为提示词全文）

        返回：
            str：保存后的提示词原文（请求体原文回显）

        异常：
            HTTPException：提示词校验失败时以 422 响应
        """
        body = (await request.body()).decode("utf-8")
        if deps.research_prompt_save is not None:
            try:
                await deps.research_prompt_save(body)
            except ResearchPromptValidationError as exc:
                raise HTTPException(status_code=422, detail="；".join(exc.reasons)) from exc
            return body
        deps.research_prompt_path.write_text(body, encoding="utf-8")
        return body

    @router.get("/research/prompt/versions")
    async def list_research_prompt_versions() -> dict[str, Any]:
        """研报提示词版本列表（最新在前）：不含 content，省流量。

        参数：无

        返回：
            dict[str, Any]，{"items": [...]}，版本项含 id/md5/created_by/reason/
            review_report_id/created_at/status，不含 content 全文
        """
        versions = await deps.repo.research_prompt.list_versions()
        return {"items": [_research_prompt_version_item(v) for v in versions]}

    @router.get("/research/prompt/versions/{version_id}")
    async def get_research_prompt_version(version_id: int) -> dict[str, Any]:
        """研报提示词版本详情：含 content 全文。

        参数：
            version_id: int，研报提示词版本编号

        返回：
            dict[str, Any]，版本完整记录（含 content 全文）

        异常：
            HTTPException，指定版本不存在时以 404 响应
        """
        return (await _get_research_prompt_version_or_404(deps, version_id)).model_dump()

    @router.get("/research/prompt/diff", response_class=PlainTextResponse)
    async def diff_research_prompt_versions(
        from_id: int = Query(alias="from"), to: int = Query()
    ) -> str:
        """两版本研报提示词 unified diff（纯文本）；参数非法 422、版本不存在 404。

        参数：
            from_id: int，起始版本编号
            to: int，目标版本编号

        返回：
            str，两版本研报提示词 unified diff（纯文本）

        异常：
            HTTPException，任一版本不存在时以 404 响应
        """
        from_version = await _get_research_prompt_version_or_404(deps, from_id)
        to_version = await _get_research_prompt_version_or_404(deps, to)
        return "\n".join(
            difflib.unified_diff(
                from_version.content.splitlines(),
                to_version.content.splitlines(),
                fromfile=f"v{from_id}",
                tofile=f"v{to}",
                lineterm="",
            )
        )

    @router.post("/research/prompt/rollback/{version_id}")
    async def rollback_research_prompt(version_id: int) -> dict[str, Any]:
        """回滚到指定研报提示词版本：回调未接线 503；版本不存在或状态非 applied（不可回滚）404。

        参数：
            version_id: int，研报提示词版本编号

        返回：
            dict[str, Any]，{"rolled_back_to", "version"}：回滚目标版本号与新版本号

        异常：
            HTTPException，研报提示词版本管理未接线时以 503 响应，
                目标版本不存在或状态非 applied（草稿/已废弃不可回滚）时以 404 响应
        """
        if deps.research_prompt_rollback is None:
            raise HTTPException(status_code=503, detail="研报提示词版本管理未接线（agent 未装配）")
        try:
            return await deps.research_prompt_rollback(version_id)
        except ResearchPromptValidationError as exc:  # 版本不存在或状态非 applied 均映 404
            raise HTTPException(status_code=404, detail="；".join(exc.reasons)) from exc

    return router
