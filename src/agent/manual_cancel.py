from __future__ import annotations

from src.agent.tool_handlers import ToolDeps
from src.audit.logger import get_logger
from src.gateway.base import OrderNotFound

logger = get_logger(__name__)


def _require_open_order(deps: ToolDeps, contract: str, order_id: str) -> None:
    offset = 0
    while True:
        page = deps.gateway.list_orders(contract, "open", limit=100, offset=offset)
        if any(order.id == order_id for order in page):
            return
        if len(page) < 100:
            break
        offset += len(page)
    raise OrderNotFound(
        "\u6302\u5355\u4e0d\u5b58\u5728\u6216\u5df2\u4e0d\u5904\u4e8e open \u72b6\u6001",
        label="ORDER_NOT_FOUND",
    )


async def execute_manual_cancel(deps: ToolDeps, contract: str, order_id: str) -> dict:
    _require_open_order(deps, contract, order_id)
    result = deps.gateway.cancel_order(contract, order_id)
    warning = ""
    try:
        await deps.repo.update_order_status(
            order_id, result.status, result.finish_as or "cancelled"
        )
    except Exception as exc:
        logger.exception("manual cancel local sync failed for %s", order_id)
        warning = (
            f"\u4ea4\u6613\u7f51\u5173\u5df2\u64a4\u5355\uff0c"
            f"\u4f46\u672c\u5730\u8bb0\u5f55\u540c\u6b65\u5931\u8d25 ({type(exc).__name__}: {exc})"
            "\uff0c\u8bf7\u52ff\u91cd\u8bd5\u64a4\u5355"
        )
    return {
        "id": result.id,
        "contract": result.contract,
        "status": result.status,
        "finish_as": result.finish_as or "cancelled",
        "warning": warning,
    }
