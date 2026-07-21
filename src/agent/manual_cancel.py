from __future__ import annotations

from src.agent.tool_handlers import ToolDeps
from src.audit.logger import get_logger
from src.gateway.base import OrderNotFound

logger = get_logger(__name__)


def _require_open_order(deps: ToolDeps, contract: str, order_id: str) -> None:
    # 分页核对订单仍是未成交状态，避免重复撤销已成交或已取消订单。
    offset = 0
    while True:
        page = deps.gateway.list_orders(contract, "open", limit=100, offset=offset)
        if any(order.id == order_id for order in page):
            return
        if len(page) < 100:
            break
        offset += len(page)
    raise OrderNotFound(
        "挂单不存在或已不处于 open 状态",
        label="ORDER_NOT_FOUND",
    )


async def execute_manual_cancel(deps: ToolDeps, contract: str, order_id: str) -> dict:
    # 撤销网关订单并同步本地记录；同步失败时返回不可重试警告。
    _require_open_order(deps, contract, order_id)
    result = deps.gateway.cancel_order(contract, order_id)
    warning = ""
    try:
        await deps.repo.update_order_status(
            order_id, result.status, result.finish_as or "cancelled"
        )
    except Exception as exc:
        logger.exception("manual cancel local sync failed for %s", order_id)
        warning = f"交易网关已撤单，但本地记录同步失败 ({type(exc).__name__}: {exc})，请勿重试撤单"
    return {
        "id": result.id,
        "contract": result.contract,
        "status": result.status,
        "finish_as": result.finish_as or "cancelled",
        "warning": warning,
    }
