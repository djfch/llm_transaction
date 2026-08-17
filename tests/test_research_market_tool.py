"""研报市场数据工具：白名单、参数与本轮快照留存契约。"""

from __future__ import annotations

import json
from typing import cast

import pytest

from src.memory.repo import Repo
from src.research.providers.base import ResearchDataProvider
from src.research.tool_handlers import ResearchToolDeps
from src.research.tools import ResearchToolRegistry


class _MarketData:
    def __init__(self) -> None:
        """初始化假行情服务，创建用于记录调用入参的空列表。

        参数：无

        返回：
            None，副作用是初始化实例属性 calls（(合约, limit) 调用记录列表）
        """
        self.calls: list[tuple[str, int]] = []

    async def snapshot(self, contract: str, limit: int = 30) -> dict:
        """模拟行情快照查询：记录入参并返回固定结构的假快照。

        参数：
            contract: str，被查询的合约代码
            limit: int，请求的数据条数上限，默认 30

        返回：
            dict：包含合约代码、实际请求的 limit 与数据状态的假快照
        """
        self.calls.append((contract, limit))
        return {"contract": contract, "requested_limit": limit, "data_status": "完整"}


class _FlakyMarketData(_MarketData):
    """首次查询抛出异常、后续查询恢复正常的假行情服务。"""

    async def snapshot(self, contract: str, limit: int = 30) -> dict:
        """模拟一次内部错误，再返回可留存的行情快照。

        参数：
            contract: str，被查询的合约代码
            limit: int，请求的数据条数上限，默认 30

        返回：
            dict：第二次及后续调用返回的完整假快照

        异常：
            RuntimeError：首次调用时模拟行情服务内部错误
        """
        self.calls.append((contract, limit))
        if len(self.calls) == 1:
            raise RuntimeError("临时行情服务错误")
        return {"contract": contract, "requested_limit": limit, "data_status": "完整"}


def _deps(service: _MarketData) -> ResearchToolDeps:
    """构造挂载假行情服务、本轮白名单为 BTC/ETH 的研报工具依赖。

    参数：
        service: _MarketData，假行情服务对象，注入为 market_data 以便断言调用情况

    返回：
        ResearchToolDeps：provider 与 repo 为无关桩对象、mode 为 paper 的测试依赖
    """
    return ResearchToolDeps(
        provider=cast(ResearchDataProvider, object()),
        repo=cast(Repo, object()),
        mode="paper",
        market_data=service,
        watchlist_snapshot=("BTC_USDT", "ETH_USDT"),
    )


@pytest.mark.asyncio
async def test_market_tool_returns_snapshot_and_records_contract() -> None:
    """校验市场数据工具正常返回快照，并把合约与快照留存到本轮依赖。

    参数：无

    返回：
        None，断言行情服务以默认 limit=30 被调用一次、返回快照原样留存到
        deps.market_snapshots、合约记入 deps.market_data_contracts，
        且工具 schema 的必填参数只有 contract
    """
    service = _MarketData()
    deps = _deps(service)
    registry = ResearchToolRegistry(deps)

    result = json.loads(
        await registry.execute("get_research_market_data", {"contract": "BTC_USDT"})
    )

    assert service.calls == [("BTC_USDT", 30)]
    assert result["contract"] == "BTC_USDT"
    assert deps.market_data_contracts == {"BTC_USDT"}
    assert deps.market_snapshots["BTC_USDT"] == result
    schema = next(item for item in registry.schemas() if item["name"] == "get_research_market_data")
    assert schema["parameters"]["required"] == ["contract"]


@pytest.mark.asyncio
async def test_market_tool_rejects_duplicate_call_and_preserves_first_snapshot() -> None:
    """校验同一合约重复调用被拒绝，且本轮留存仍是首次查询的快照。

    参数：无

    返回：
        None，断言第二次调用返回"重复调用"提示、行情服务仅被首次
        limit=100 调用一次、deps.market_snapshots 中保留首次快照
    """
    service = _MarketData()
    deps = _deps(service)
    registry = ResearchToolRegistry(deps)

    first = json.loads(
        await registry.execute("get_research_market_data", {"contract": "BTC_USDT", "limit": 100})
    )
    second = await registry.execute(
        "get_research_market_data", {"contract": "BTC_USDT", "limit": 1}
    )

    assert "重复调用" in second
    assert service.calls == [("BTC_USDT", 100)]
    assert deps.market_snapshots["BTC_USDT"] == first


@pytest.mark.asyncio
async def test_market_tool_allows_retry_after_internal_error_then_rejects_duplicate() -> None:
    """校验失败尝试不记为成功，重试成功后才启用重复调用保护。

    参数：无

    返回：
        None，断言首次内部错误显式返回且不留存，第二次调用成功留存，
        第三次被拒绝且行情服务不再收到请求
    """
    service = _FlakyMarketData()
    deps = _deps(service)
    registry = ResearchToolRegistry(deps)

    failed = await registry.execute(
        "get_research_market_data", {"contract": "BTC_USDT", "limit": 20}
    )

    assert "工具内部错误" in failed
    assert "临时行情服务错误" in failed
    assert deps.market_data_contracts == set()
    assert deps.market_snapshots == {}

    succeeded = json.loads(
        await registry.execute("get_research_market_data", {"contract": "BTC_USDT", "limit": 20})
    )
    duplicate = await registry.execute(
        "get_research_market_data", {"contract": "BTC_USDT", "limit": 20}
    )

    assert succeeded["data_status"] == "完整"
    assert deps.market_data_contracts == {"BTC_USDT"}
    assert deps.market_snapshots["BTC_USDT"] == succeeded
    assert "重复调用" in duplicate
    assert service.calls == [("BTC_USDT", 20), ("BTC_USDT", 20)]


@pytest.mark.asyncio
async def test_market_tool_rejects_contract_outside_run_snapshot() -> None:
    """校验本轮白名单外的合约被拒绝，且不触发任何行情查询。

    参数：无

    返回：
        None，断言返回"不在本轮白名单"提示、行情服务零调用、
        deps.market_data_contracts 保持为空集合
    """
    service = _MarketData()
    deps = _deps(service)

    result = await ResearchToolRegistry(deps).execute(
        "get_research_market_data", {"contract": "SOL_USDT", "limit": 20}
    )

    assert "不在本轮白名单" in result
    assert service.calls == []
    assert deps.market_data_contracts == set()


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, 101, True, 1.5])
async def test_market_tool_rejects_invalid_limit(limit: object) -> None:
    """校验非法 limit（越界或非正整数类型）被拒绝，且不触发行情查询。

    参数：
        limit: object，pytest 参数化注入的非法 limit 取值：0、101、True、1.5

    返回：
        None，断言返回内容包含 limit 的报错提示且行情服务零调用
    """
    service = _MarketData()
    deps = _deps(service)

    result = await ResearchToolRegistry(deps).execute(
        "get_research_market_data", {"contract": "BTC_USDT", "limit": limit}
    )

    assert "limit" in result
    assert service.calls == []
