"""交易写操作与行情 K 线端点：手动平仓、模拟账户重置、agent 启停、K 线查询。

写端点只经 ServerDeps 注入的回调触达 agent/网关层（server 不 import 具体实现）；
未接线时 503（回调缺失）/409（模式不支持），交易所 API key 永不进响应。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from src.config import load_watchlist
from src.config_io import read_settings_raw, write_settings
from src.gateway.base import GatewayError
from src.server.deps import ServerDeps

# K 线周期白名单（与 Gate 支持的粒度对齐）
_CANDLE_INTERVALS = frozenset({"1m", "5m", "15m", "1h", "4h", "1d"})


class PaperResetBody(BaseModel):
    """模拟账户重置请求：equity 为新的初始权益（必须为正，金额 Decimal 解析）。"""

    equity: Decimal = Field(gt=0)


def _watchlist_contracts(deps: ServerDeps) -> list[str]:
    """当前生效的合约名单：运行时共享名单优先，未接线时读 watchlist.yaml。"""
    if deps.runtime_watchlist is not None:
        return deps.runtime_watchlist
    try:
        return load_watchlist(deps.watchlist_path).contracts
    except ValueError:
        return []  # 名单文件缺失/非法：按空名单处理，随后的合约校验统一 422


def _agent_running(deps: ServerDeps) -> bool:
    """agent 真实运行态：经 status_provider 读取；未注入/缺字段时为 False（不硬编码）。"""
    return bool(deps.runtime_status().get("agent_running", False))


def create_trading_router(deps: ServerDeps) -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.post("/positions/{contract}/close")
    async def close_position(contract: str) -> dict[str, Any]:
        """手动平仓：与 LLM 平仓同一风控路径（由注入回调保证）。"""
        if deps.manual_close is None:
            raise HTTPException(status_code=503, detail="手动平仓未接线（agent 未注入）")
        try:
            return await deps.manual_close(contract)
        except GatewayError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except Exception as exc:
            # 风控拒绝等平仓前置失败：回调抛出的异常消息即原因文本
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/paper/reset")
    async def reset_paper(body: PaperResetBody) -> dict[str, Any]:
        """重置模拟账户：清空模拟仓位/挂单并重设初始权益（写回 config.yaml）。"""
        settings = deps.runtime_settings
        if settings is None or settings.mode != "paper" or deps.paper_reset is None:
            raise HTTPException(status_code=409, detail="仅 paper 模式且已接线模拟账户时可重置")
        raw = read_settings_raw(deps.config_path)
        raw.setdefault("paper", {})["initial_equity"] = body.equity
        try:
            write_settings(raw, deps.config_path)  # 先落盘：写回失败则账户无任何副作用
        except ValueError as exc:  # ConfigError（整体校验失败）
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        deps.paper_reset(body.equity)
        settings.paper.initial_equity = body.equity  # 原地更新共享实例，下轮决策即生效
        return {"equity": float(body.equity)}

    @router.post("/agent/start")
    async def start_agent() -> dict[str, bool]:
        if deps.agent_start is None:
            raise HTTPException(status_code=503, detail="agent 调度未接线")
        await deps.agent_start()
        return {"agent_running": _agent_running(deps)}

    @router.post("/agent/stop")
    async def stop_agent() -> dict[str, bool]:
        if deps.agent_stop is None:
            raise HTTPException(status_code=503, detail="agent 调度未接线")
        await deps.agent_stop()
        return {"agent_running": _agent_running(deps)}

    @router.get("/candles")
    async def get_candles(
        contract: str = Query(...),
        interval: str = Query("1h"),
        limit: int = Query(200, ge=1, le=1000),
    ) -> dict[str, Any]:
        """K 线查询：合约须在 watchlist 内；interval 白名单校验；返回图表消费的 number。"""
        if contract not in _watchlist_contracts(deps):
            raise HTTPException(status_code=422, detail=f"合约不在 watchlist: {contract}")
        if interval not in _CANDLE_INTERVALS:
            raise HTTPException(status_code=422, detail=f"非法 K 线周期: {interval}")
        if deps.gateway is None:
            raise HTTPException(status_code=503, detail="交易网关未就绪（agent 未接线）")
        try:
            candles = deps.gateway.get_candlesticks(contract, interval=interval, limit=limit)
        except GatewayError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        items = [
            {
                "t": c.t,
                "o": float(c.o),
                "h": float(c.h),
                "l": float(c.l),
                "c": float(c.c),
                "v": float(c.v),
            }
            for c in candles
        ]
        return {"items": items}

    return router
