"""只读监控端点：运行状态、账户/持仓、决策轮、成交、权益曲线、笔记。"""

from __future__ import annotations

import json
import time
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from src.config import Settings, load_settings
from src.gateway.base import Gateway, GatewayError
from src.memory.models import AuditRound, AuditToolCall
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


def _parse_json_field(raw: str) -> Any:
    """审计落库的 args_json/result_json 还原为对象；非法 JSON 保留原字符串。"""
    try:
        return json.loads(raw)
    except ValueError:  # json.JSONDecodeError 是 ValueError 子类
        return raw


def _tool_call_item(call: AuditToolCall) -> dict[str, Any]:
    """实时展示的单次工具调用（键与 /api/agent/live 前端契约逐字对齐）。"""
    return {
        "seq": call.seq,
        "tool": call.tool,
        "args": _parse_json_field(call.args_json),
        "risk_verdict": call.risk_verdict,
        "risk_reason": call.risk_reason,
        "result": _parse_json_field(call.result_json),
        "duration_ms": call.duration_ms,
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


def _day_start_ts() -> float:
    """服务器本地时区当日 0 点（与 agent 侧 default_daily_stats 同一自然日口径）。"""
    now = time.localtime()
    return time.mktime((now.tm_year, now.tm_mon, now.tm_mday, 0, 0, 0, 0, 0, -1))


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
            "llm_configured": runtime.get("llm_configured", False),
        }

    @router.get("/agent/live")
    async def get_agent_live() -> dict[str, Any]:
        """实时决策展示：当前模式最新一轮审计 + 已落库工具调用（响应键与前端契约冻结）。

        in_round 取运行时状态的 in_round 键（调度器防重入标记），缺省 False；
        mode 优先 runtime_settings（agent 实际运行模式），未接线回退配置文件；
        进行中的轮 ended_at 为 null、llm_raw 为空串；无轮次时 round 为 null。
        in_round 与 round 非原子配对：调度器标记先于/晚于审计行落库，存在 ms 级
        窗口（in_round=true 时可能短暂返回上一已结束轮），前端应以 round.ended_at
        为轮次进行中的最终判据；配对一致性依赖单调度器单决策循环的构造不变量
        （bootstrap 接线保证）。
        """
        runtime = deps.runtime_status()
        settings = deps.runtime_settings or load_settings(deps.config_path)
        round_row = await deps.repo.latest_audit_round(settings.mode)
        if round_row is None:
            return {"in_round": runtime.get("in_round", False), "round": None, "tool_calls": []}
        calls = await deps.repo.list_audit_tool_calls(round_row.round_id)
        return {
            "in_round": runtime.get("in_round", False),
            "round": round_row.model_dump(exclude={"mode"}),  # 契约不含 mode 键
            "tool_calls": [_tool_call_item(c) for c in calls],
        }

    @router.get("/account")
    async def get_account() -> dict[str, Any]:
        """账户概览：available/unrealised_pnl + equity（前端 AccountInfo 契约要求 equity 必在）。

        equity = available + Σ持仓保证金 + 未实现盈亏；持仓查询失败时降级为
        available + unrealised_pnl（近似值，保证前端永远拿到数字）。
        """
        account = _require_gateway(deps).get_account().model_dump()
        equity = _account_equity(deps)
        account["equity"] = (
            equity if equity is not None else account["available"] + account["unrealised_pnl"]
        )
        return account

    @router.get("/positions")
    async def get_positions() -> list[dict[str, Any]]:
        return [p.model_dump() for p in _require_gateway(deps).list_positions()]

    @router.get("/daily_stats")
    async def get_daily_stats() -> dict[str, Any]:
        """当日统计（风控同一口径的只读暴露）：服务器时区自然日、按当前 mode 过滤、
        orders_today 仅开仓单（is_close 排除）；realized_pnl 为当日已实现盈亏合计（未扣费）。
        前端账户面板据此替代本地成交口径估算，上限取 risk.max_orders_per_day。
        """
        settings = deps.runtime_settings or load_settings(deps.config_path)
        stats = await deps.repo.daily_stats(settings.mode, _day_start_ts())
        return {
            "realized_pnl": _cents(stats.realized_pnl),
            "orders_today": stats.orders_today,
            "max_orders_per_day": settings.risk.max_orders_per_day,
        }

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
        """决策轮详情：round 字段展平到顶层 + 工具调用 args/result 解析为对象。

        响应形态与前端 RoundDetail 类型逐字对齐（此前嵌套 round/args_json 的形态
        会让前端读顶层字段时崩页，契约测试锁定本形态）。
        """
        if deps.audit_trail is None:
            raise HTTPException(status_code=503, detail="审计追踪未配置")
        data = await deps.audit_trail.get_round(round_id)
        if data is None:
            raise HTTPException(status_code=404, detail=f"决策轮不存在: {round_id}")
        calls = await deps.repo.list_audit_tool_calls(round_id)
        return {**data["round"], "tool_calls": [_tool_call_item(c) for c in calls]}

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
