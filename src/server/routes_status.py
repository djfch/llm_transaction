"""只读监控端点：运行状态、账户/持仓、决策轮、成交、权益曲线、笔记、价格唤醒。"""

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
    """gateway 未注入（agent 未接线）时返回 503 而非 500。

    参数：
        deps: ServerDeps，当前服务端运行依赖

    返回：
        Gateway：gateway 未注入（agent 未接线）时返回 503 而非 500

    异常：
        HTTPException：deps.gateway 未注入时抛出 503
    """
    if deps.gateway is None:
        raise HTTPException(status_code=503, detail="交易网关未就绪（agent 未接线）")
    return deps.gateway


def _audit_summary(audit: AuditRound | None) -> dict[str, Any] | None:
    """决策轮列表里的审计摘要（不含 prompt/上下文全文，避免列表过大）。

    参数：
        audit: AuditRound | None，待转换为列表摘要的审计轮；为空表示无记录

    返回：
        dict[str, Any] | None：决策轮列表里的审计摘要（不含 prompt/上下文全文，避免列表过大）
    """
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
    """审计落库的 args_json/result_json 还原为对象；非法 JSON 保留原字符串。

    参数：
        raw: str，审计表保存的 JSON 文本

    返回：
        Any：审计落库的 args_json/result_json 还原为对象；非法 JSON 保留原字符串
    """
    try:
        return json.loads(raw)
    except ValueError:  # json.JSONDecodeError 是 ValueError 子类
        return raw


def _tool_call_item(call: AuditToolCall) -> dict[str, Any]:
    """实时展示的单次工具调用（键与 /api/agent/live 前端契约逐字对齐）。

    参数：
        call: AuditToolCall，待转换为实时展示结构的审计工具调用

    返回：
        dict[str, Any]：实时展示的单次工具调用（键与 /api/agent/live 前端契约逐字对齐）
    """
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

    参数：
        value: Decimal，需要转换为展示金额的 Decimal 数值

    返回：
        float：金额转展示用 number：四舍五入到分位，避免 float() 直转的浮点长尾
    """
    return float(value.quantize(Decimal("0.01")))


def _account_equity(deps: ServerDeps) -> Decimal | None:
    """账户当前权益估值（可用 + 持仓保证金 + 未实现盈亏）；未接线或查询失败返回 None。

    参数：
        deps: ServerDeps，当前服务端运行依赖

    返回：
        Decimal | None：账户当前权益估值（可用 + 持仓保证金 + 未实现盈亏）；未接线或查询失败返回 None
    """
    if deps.gateway is None:
        return None
    try:
        account = deps.gateway.get_account()
        margin = sum((p.margin for p in deps.gateway.list_positions()), Decimal(0))
    except GatewayError:
        return None
    return account.available + margin + account.unrealised_pnl


def _day_start_ts() -> float:
    """服务器本地时区当日 0 点（与 agent 侧 default_daily_stats 同一自然日口径）。

    参数：
        无

    返回：
        float：服务器本地时区当日 0 点（与 agent 侧 default_daily_stats 同一自然日口径）
    """
    now = time.localtime()
    return time.mktime((now.tm_year, now.tm_mon, now.tm_mday, 0, 0, 0, 0, 0, -1))


def _equity_baseline(
    deps: ServerDeps, settings: Settings, pnl_fee_sum: Decimal
) -> tuple[Decimal, str]:
    """权益曲线基准与来源标注。

    paper 模式用配置的初始权益；testnet/live 由账户当前权益倒推起点
    （当前权益 - 曲线累计净盈亏，使曲线末端恰为当前权益，避免与历史盈亏重复计数）；
    账户不可用时降级为 0（曲线只反映相对变化）并在响应中标注。

    参数：
        deps: ServerDeps，当前服务端运行依赖
        settings: Settings，当前运行配置
        pnl_fee_sum: Decimal，成交累计已实现盈亏与手续费净额

    返回：
        tuple[Decimal, str]：权益曲线基准与来源标注
    """
    if settings.mode == "paper":
        return settings.paper.initial_equity, "paper_config"
    equity_now = _account_equity(deps)
    if equity_now is None:
        return Decimal(0), "fallback_zero"
    return equity_now - pnl_fee_sum, "account"


def _trader_llm_status(settings: Settings, runtime: dict[str, Any]) -> dict[str, str]:
    """优先读取实际已加载凭证，未接线时解析决策 Agent 当前配置。

    参数：
        settings: Settings，当前生效的完整运行配置
        runtime: dict[str, Any]，运行状态提供器返回的实际 Provider 摘要

    返回：
        dict[str, str]，实际或配置回退的凭证名、提供商、模型名和思考强度
    """
    keys = (
        "llm_credential_name",
        "llm_provider",
        "llm_model",
        "llm_thinking_effort",
    )
    if all(key in runtime for key in keys):
        return {key: str(runtime[key]) for key in keys}
    credential_name = settings.agents.trader.credential
    credential = next(
        item for item in settings.llm.resolve_credentials() if item.name == credential_name
    )
    return {
        "llm_credential_name": credential.name,
        "llm_provider": credential.provider,
        "llm_model": credential.model,
        "llm_thinking_effort": credential.thinking_effort,
    }


def create_status_router(deps: ServerDeps) -> APIRouter:
    """创建只读监控路由：运行状态、账户/持仓、决策轮、成交、权益曲线、笔记、价格唤醒。

    参数：
        deps: ServerDeps，服务装配依赖（网关、仓库、运行时状态、审计追踪、预警提供者等）

    返回：
        APIRouter：挂载全部只读监控端点的路由（前缀 /api），由主应用注册
    """
    router = APIRouter(prefix="/api")

    @router.get("/status")
    async def get_status() -> dict[str, Any]:
        """运行状态概览：模式、运行时长、熔断开关、agent 运行态与 LLM 配置。

        mode 与 LLM 优先读共享运行配置，未接线时才回退配置文件；
        uptime 由运行时提供者给出；
        kill_switch 优先取运行时内存值（agent 连续失败触发的内存态锁不写文件）。

        参数：无

        返回：
            dict[str, Any]：含 mode/uptime_seconds/kill_switch/agent_running、
            决策凭证名/provider/model/thinking_effort 与 llm_configured 的状态字典
        """
        settings = deps.runtime_settings or load_settings(deps.config_path)
        runtime = deps.runtime_status()
        llm_status = _trader_llm_status(settings, runtime)
        return {
            "mode": settings.mode,
            "uptime_seconds": runtime.get("uptime_seconds", 0),
            "kill_switch": runtime.get("kill_switch", settings.risk.kill_switch),
            "agent_running": runtime.get("agent_running", False),
            **llm_status,
            "llm_configured": runtime.get("llm_configured", False),
        }

    @router.get("/agent/live")
    async def get_agent_live() -> dict[str, Any]:
        """实时决策展示：当前模式最新一轮交易审计（排除复盘/研报轮）+ 已落库工具调用（响应键与前端契约冻结）。

        in_round 取运行时状态的 in_round 键（调度器防重入标记），缺省 False；
        mode 优先 runtime_settings（agent 实际运行模式），未接线回退配置文件；
        进行中的轮 ended_at 为 null、llm_raw 为空串；无轮次时 round 为 null。
        in_round 与 round 非原子配对：调度器标记先于/晚于审计行落库，存在 ms 级
        窗口（in_round=true 时可能短暂返回上一已结束轮），前端应以 round.ended_at
        为轮次进行中的最终判据；配对一致性依赖单调度器单决策循环的构造不变量
        （bootstrap 接线保证）。

        参数：
            无

        返回：
            dict[str, Any]：实时决策展示：当前模式最新一轮交易审计（排除复盘/研报轮）+ 已落库工具调用（响应键与前端契约冻结）
        """
        runtime = deps.runtime_status()
        settings = deps.runtime_settings or load_settings(deps.config_path)
        # trader 视图只看交易轮：复盘/研报轮各有专属端点（/api/review/live、/api/research/live），
        # 不排除时复盘/研报轮更新（更新更频繁）会污染交易实时展示
        round_row = await deps.repo.latest_audit_round(
            settings.mode, exclude_wake_sources=("review", "research")
        )
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

        参数：
            无

        返回：
            dict[str, Any]：账户概览：available/unrealised_pnl + equity（前端 AccountInfo 契约要求 equity 必在）
        """
        account = _require_gateway(deps).get_account().model_dump()
        equity = _account_equity(deps)
        account["equity"] = (
            equity if equity is not None else account["available"] + account["unrealised_pnl"]
        )
        return account

    @router.get("/positions")
    async def get_positions() -> list[dict[str, Any]]:
        """当前全部持仓列表，直接透传交易网关的查询结果。

        参数：无

        返回：
            list[dict[str, Any]]：持仓字典列表；无持仓时为空列表
        """
        return [p.model_dump() for p in _require_gateway(deps).list_positions()]

    @router.get("/portfolio")
    async def get_portfolio() -> dict[str, Any]:
        """一次读取账户与持仓，返回同一时点的权威组合快照。

        参数：
            无

        返回：
            dict[str, Any]：一次读取账户与持仓，返回同一时点的权威组合快照
        """
        gateway = _require_gateway(deps)
        positions = gateway.list_positions()
        account = gateway.get_account().model_dump()
        margin = sum((position.margin for position in positions), Decimal(0))
        account["equity"] = account["available"] + margin + account["unrealised_pnl"]
        return {
            "as_of": time.time(),
            "account": account,
            "positions": [position.model_dump() for position in positions],
        }

    @router.get("/daily_stats")
    async def get_daily_stats() -> dict[str, Any]:
        """当日统计（风控同一口径的只读暴露）：服务器时区自然日、按当前 mode 过滤、
        orders_today 仅开仓单（is_close 排除）；realized_pnl 为当日已实现盈亏合计（未扣费）。
        前端账户面板据此替代本地成交口径估算，上限取 risk.max_orders_per_day。

        参数：
            无

        返回：
            dict[str, Any]：当日统计（风控同一口径的只读暴露）：服务器时区自然日、按当前 mode 过滤、
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
        """分页返回决策轮摘要及总数；列表不含 LLM 原文，含每轮归属笔记引文（无归属为 null）。

        参数：
            offset: int，分页起始偏移量
            limit: int，每页最多返回的记录数量

        返回：
            dict[str, Any]：分页返回决策轮摘要及总数；列表不含 LLM 原文，含每轮归属笔记引文（无归属为 null）
        """
        decisions, total = await deps.repo.list_decisions_page(limit=limit, offset=offset)
        round_ids = [d.round_id for d in decisions]
        audits = await deps.repo.list_audit_rounds(round_ids)  # 批量取，免 N+1
        notes = await deps.repo.list_notes_by_rounds(round_ids)  # 同页笔记引文（每轮最新一条）
        items = []
        for d in decisions:
            item = d.model_dump()
            item.pop("llm_raw", None)  # 列表不返回 LLM 原文，详情走 /rounds/{id}
            item["audit"] = _audit_summary(audits.get(d.round_id))
            note = notes.get(d.round_id)
            item["note"] = (
                {"content": note.content, "created_at": note.created_at} if note else None
            )
            items.append(item)
        return {"offset": offset, "limit": limit, "total": total, "items": items}

    @router.get("/rounds/{round_id}")
    async def get_round(round_id: str) -> dict[str, Any]:
        """决策轮详情：round 字段展平到顶层 + 工具调用 args/result 解析为对象。

            响应形态与前端 RoundDetail 类型逐字对齐，契约测试锁定本形态。

            参数：
                round_id: str，决策轮编号

            返回：
                dict[str, Any]：决策轮详情：round 字段展平到顶层 + 工具调用 args/result 解析为对象

        异常：
            HTTPException：审计追踪未配置时抛出 503；指定轮次不存在时抛出 404
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
        """分页查询历史成交记录，可按合约过滤。

        分页与合约过滤都在 SQL 层完成；total 为同过滤条件下的总条数（前端分页器用）。

        参数：
            offset: int，分页起始偏移，省略时从第 0 条开始
            limit: int，每页条数（1–200），省略时取 50
            contract: str | None，合约名过滤（如 BTC_USDT），省略时返回全部合约

        返回：
            dict[str, Any]：含 items（成交列表）/total/offset/limit 的分页结果
        """
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
        """账户权益曲线：基准权益起步，按成交逐笔累计净盈亏得到折线点。

        基准按模式选取（paper 取配置初始权益，testnet/live 由账户当前权益倒推，账户不可用
        降级为 0）；逐笔累计口径为已实现盈亏 - 手续费，成交按当前模式过滤。

        参数：无

        返回：
            dict[str, Any]：含 initial_equity（基准权益）、baseline_source（基准来源标注）、
            points（时间与权益组成的曲线点列表，无成交时为当前时点的单点）
        """
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

    @router.get("/alerts")
    async def list_alerts() -> list[dict[str, Any]]:
        """LLM 设置的未触发价格唤醒（内存唯一存储），供前端「价格唤醒」面板展示。

            预警线只存于 TriggerManager 内存索引：触发即移除并唤醒 agent，进程重启即失效，
            故面板只暴露当前未触发的条目。响应契约保持原 alerts 表形态
            （id 数字 / direction above|below / price 字符串 / created_at Unix 秒 / active 恒 true）。

            参数：
                无

            返回：
                list[dict[str, Any]]：LLM 设置的未触发价格唤醒（内存唯一存储），供前端「价格唤醒」面板展示

        异常：
            HTTPException：价格唤醒管理器未接线时抛出 503
        """
        if deps.alerts_provider is None:
            raise HTTPException(status_code=503, detail="价格唤醒未接线（agent 未就绪）")
        return [
            {
                "id": t.id,
                "contract": t.contract,
                "direction": "above" if t.direction == ">=" else "below",
                "price": str(t.price),  # 锁字符串形态（原 pydantic Decimal 序列化口径）
                "active": True,  # 内存索引只存未触发条目，恒真以保持响应形态
                "created_at": t.created_at,
            }
            for t in sorted(deps.alerts_provider(), key=lambda x: x.id)
        ]

    @router.get("/notes")
    async def get_notes(
        offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=200)
    ) -> dict[str, Any]:
        """分页返回最新在前的 Agent 笔记及总数，不改变 Agent 上下文的 recent_notes 顺序。

        参数：
            offset: int，分页起始偏移量
            limit: int，每页最多返回的记录数量

        返回：
            dict[str, Any]：分页返回最新在前的 Agent 笔记及总数，不改变 Agent 上下文的 recent_notes 顺序
        """
        notes, total = await deps.repo.list_notes_page(limit=limit, offset=offset)
        return {
            "items": [n.model_dump() for n in notes],
            "total": total,
            "offset": offset,
            "limit": limit,
        }

    return router
