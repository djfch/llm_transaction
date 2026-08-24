"""未成交订单撤销与本地状态同步。"""

from src.agent.tool_handlers import ToolDeps, ToolOutcome, _need_str
from src.gateway.async_io import PRIORITY_HIGH, run_gateway_io


async def cancel_order(deps: ToolDeps, args: dict) -> ToolOutcome:
    """撤销交易所挂单并同步本地订单状态。

    参数：
        deps: ToolDeps，工具依赖
        args: dict，含合约名与订单 ID

    返回：
        ToolOutcome：撤单结果
    """
    contract = _need_str(args, "contract")
    order_id = _need_str(args, "order_id")
    result = await run_gateway_io(
        deps.gateway.cancel_order, contract, order_id, priority=PRIORITY_HIGH, mutation=True
    )
    await deps.repo.update_order_status(order_id, result.status, result.finish_as or "cancelled")
    return ToolOutcome(f"撤单成功：订单 {order_id}，状态 {result.status}")
