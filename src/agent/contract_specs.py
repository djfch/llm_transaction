"""新增敞口相关的实时 Gate 合约规格读取。"""

from __future__ import annotations

from src.agent.tool_handlers import ToolArgError, ToolDeps
from src.gateway.async_io import run_gateway_io
from src.gateway.base import Contract, GatewayError


async def fresh_contract(deps: ToolDeps, contract: str) -> Contract:
    """读取对应环境的实时 Gate 规格；paper 同步更新模拟撮合内存。

    参数：
        deps: ToolDeps，工具依赖
        contract: str，合约标识

    返回：
        Contract：最新合约规格

    异常：
        ToolArgError：合约不在交易状态或正在下架时抛出
    """
    provider = getattr(deps.gateway, "refresh_contract", deps.gateway.get_contract)
    meta = await run_gateway_io(provider, contract)
    if meta.status != "trading" or meta.in_delisting:
        raise ToolArgError(f"合约 {contract} 当前不可交易或正在下架")
    return meta


async def cached_contract(deps: ToolDeps, contract: str) -> Contract | None:
    """读取当前进程最近成功使用的合约规格，不访问官方接口。

    参数：
        deps: ToolDeps，工具依赖
        contract: str，合约标识

    返回：
        Contract | None：可用于降险计算的内存规格；尚无缓存时返回 None
    """
    provider = getattr(deps.gateway, "get_cached_contract", None)
    if provider is None:
        return None
    try:
        return await run_gateway_io(provider, contract)
    except GatewayError:
        return None
