from __future__ import annotations

from src.agent.tool_handlers import ToolDeps
from src.audit.logger import get_logger
from src.gateway.async_io import PRIORITY_HIGH, run_gateway_io
from src.gateway.base import GatewayError, OrderNotFound

logger = get_logger(__name__)

_MAX_OPEN_ORDER_PAGES = 50  # 分页核对 open 订单的总页数上限（单页 100 条），防分页异常死循环


async def _require_open_order(deps: ToolDeps, contract: str, order_id: str) -> None:
    """分页核对指定订单仍处于未成交（open）状态，避免重复撤销已成交或已取消的订单。

    逐页单独经统一卸载层 await（PRIORITY_HIGH）：既不阻塞事件循环，也让其他高优
    安全任务能在页与页之间插队，避免整段分页作为单个 executor 任务独占唯一网关
    线程（PR #84 评审 P1）；分页总页数受 _MAX_OPEN_ORDER_PAGES 约束，防交易所
    分页异常导致死循环。

    参数：
        deps: ToolDeps，工具依赖集合，使用其中的 gateway 分页查询未成交订单
        contract: str，合约名（如 BTC_USDT）
        order_id: str，待核对的订单 ID

    返回：
        None，订单仍处于 open 状态时正常返回；无其他副作用

    异常：
        OrderNotFound：订单不在未成交列表中（不存在、已成交或已取消）时抛出
        GatewayError：分页页数超过 _MAX_OPEN_ORDER_PAGES 上限（label=PAGINATION_OVERFLOW）时抛出
    """
    offset = 0
    for _ in range(_MAX_OPEN_ORDER_PAGES):
        page = await run_gateway_io(
            deps.gateway.list_orders,
            contract,
            "open",
            limit=100,
            offset=offset,
            priority=PRIORITY_HIGH,
        )
        if any(order.id == order_id for order in page):
            return
        if len(page) < 100:
            break
        offset += len(page)
    else:
        raise GatewayError(
            f"分页核对 open 订单超过 {_MAX_OPEN_ORDER_PAGES} 页上限，疑似分页异常",
            label="PAGINATION_OVERFLOW",
        )
    raise OrderNotFound(
        "挂单不存在或已不处于 open 状态",
        label="ORDER_NOT_FOUND",
    )


async def execute_manual_cancel(deps: ToolDeps, contract: str, order_id: str) -> dict:
    """执行人工撤单：撤销交易所网关订单并同步本地订单记录；同步失败时返回不可重试警告。

    参数：
        deps: ToolDeps，工具依赖集合，使用其中的 gateway 撤单、repo 更新本地订单状态
        contract: str，合约名（如 BTC_USDT）
        order_id: str，待撤销的订单 ID

    返回：
        dict：撤单结果，包含 id（订单 ID）、contract（合约名）、status（订单状态）、
        finish_as（结束方式，缺省为 cancelled）、warning（本地同步失败时的警告文案，
        成功时为空字符串）
    """
    await _require_open_order(deps, contract, order_id)
    result = await run_gateway_io(
        deps.gateway.cancel_order, contract, order_id, priority=PRIORITY_HIGH
    )
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
