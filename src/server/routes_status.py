"""只读监控端点：运行状态、账户/持仓、决策轮、成交、权益曲线、笔记。"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from src.config import Settings, load_settings
from src.gateway.base import Gateway, GatewayError
from src.memory.models import AuditRound
from src.server.deps import ServerDeps


def _require_gateway(deps: ServerDeps) -> Gateway:
    """gateway 未注入（agent 未接线）时返回 503 而非 500。"""
    if deps.gateway is None:
        raise HTTPException(status_code=503, detail="交易网关未就绪（agent 未接线）")
    return deps.gateway


def _audit_summary(audit: AuditRound | None) -> dict[str, Any] | None:
    """决策轮列表里的审计摘要（不含 prompt/上下文全文，避免列表过大）。"""
    if audit is None:
        return None
    return {
        "round_id": audit.round_id,
        "prompt_md5": audit.prompt_md5,
        "started_at": audit.started_at,
        "ended_at": audit.ended_at,
        "error": audit.error,
    }


def _cents(value: Decimal) -> float:
    """金额转展示用 number：四舍五入到分位，避免 float() 直转的浮点长尾。

    前端 lightweight-charts 需要 number；分位精度对权益曲线展示足够。
    """
    return float(value.quantize(Decimal("0.01")))


def _account_equity(deps: ServerDeps) -> Decimal | None:
    """账户当前权益估值（可用 + 持仓保证金 + 未实现盈亏）；未接线或查询失败返回 None。"""
    if deps.gateway is None:
        return None
    try:
        account = deps.gateway.get_account()
        margin = sum((p.margin for p in deps.gateway.list_positions()), Decimal(0))
    except GatewayError:
        return None
    return account.available + margin + account.unrealised_pnl


def _equity_baseline(
    deps: ServerDeps, settings: Settings, pnl_fee_sum: Decimal
) -> tuple[Decimal, str]:
    """权益曲线基准与来源标注。

    paper 模式用配置的初始权益；testnet/live 由账户当前权益倒推起点
    （当前权益 - 曲线累计净盈亏，使曲线末端恰为当前权益，避免与历史盈亏重复计数）；
    账户不可用时降级为 0（曲线只反映相对变化）并在响应中标注。
    """
    if settings.mode == "paper":
        return settings.paper.initial_equity, "paper_config"
    equity_now = _account_equity(deps)
    if equity_now is None:
        return Decimal(0), "fallback_zero"
    return equity_now - pnl_fee_sum, "account"


def create_status_router(deps: ServerDeps) -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.get("/status")
    async def get_status() -> dict[str, Any]:
        # mode/llm 读 config.yaml 当前值，uptime 由运行时提供者给出；
        # kill_switch 优先取运行时内存值（agent 连续失败触发的内存态锁不写文件）
        settings = load_settings(deps.config_path)
        runtime = deps.runtime_status()
        return {
            "mode": settings.mode,
            "uptime_seconds": runtime.get("uptime_seconds", 0),
            "kill_switch": runtime.get("kill_switch", settings.risk.kill_switch),
            "agent_running": runtime.get("agent_running", False),
            "llm_provider": settings.llm.provider,
            "llm_model": settings.llm.model,
        }

    @router.get("/account")
    async def get_account() -> dict[str, Any]:
        return _require_gateway(deps).get_account().model_dump()

    @router.get("/positions")
    async def get_positions() -> list[dict[str, Any]]:
        return [p.model_dump() for p in _require_gateway(deps).list_positions()]

    @router.get("/rounds")
    async def list_rounds(
        offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200)
    ) -> dict[str, Any]:
        decisions = await deps.repo.list_decisions(limit=limit, offset=offset)
        audits = await deps.repo.list_audit_rounds(
            [d.round_id for d in decisions]
        )  # 批量取，免 N+1
        items = []
        for d in decisions:
            item = d.model_dump()
            item.pop("llm_raw", None)  # 列表不返回 LLM 原文，详情走 /rounds/{id}
            item["audit"] = _audit_summary(audits.get(d.round_id))
            items.append(item)
        return {"offset": offset, "limit": limit, "items": items}

    @router.get("/rounds/{round_id}")
    async def get_round(round_id: str) -> dict[str, Any]:
        if deps.audit_trail is None:
            raise HTTPException(status_code=503, detail="审计追踪未配置")
        data = await deps.audit_trail.get_round(round_id)
        if data is None:
            raise HTTPException(status_code=404, detail=f"决策轮不存在: {round_id}")
        return data

    @router.get("/trades")
    async def list_trades(
        offset: int = Query(0, ge=0),
        limit: int = Query(50, ge=1, le=200),
        contract: str | None = None,
    ) -> dict[str, Any]:
        # 分页与合约过滤都在 SQL 层完成；total 为同过滤条件下的总条数（前端分页器用）
        trades = await deps.repo.list_trades(limit=limit, offset=offset, contract=contract)
        total = await deps.repo.count_trades(contract=contract)
        return {
            "items": [t.model_dump() for t in trades],
            "total": total,
            "offset": offset,
            "limit": limit,
        }

    @router.get("/equity")
    async def get_equity() -> dict[str, Any]:
        # 权益曲线：基准（按模式选取）+ 逐笔累计（已实现盈亏 - 手续费），成交按当前模式过滤
        settings = load_settings(deps.config_path)
        trades = await deps.repo.trades_between(0, time.time() + 1, mode=settings.mode)
        pnl_fee_sum = sum((t.pnl - t.fee for t in trades), Decimal(0))
        baseline, source = _equity_baseline(deps, settings, pnl_fee_sum)
        equity = baseline
        points = []
        for t in trades:
            equity += t.pnl - t.fee
            points.append({"t": t.created_at, "equity": _cents(equity)})
        if not points:
            points.append({"t": time.time(), "equity": _cents(equity)})
        return {
            "initial_equity": _cents(baseline),
            "baseline_source": source,
            "points": points,
        }

    @router.get("/notes")
    async def get_notes(limit: int = Query(20, ge=1, le=100)) -> dict[str, Any]:
        notes = await deps.repo.recent_notes(limit)
        return {"items": [n.model_dump() for n in notes]}

    return router
