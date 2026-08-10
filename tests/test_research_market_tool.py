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
        self.calls: list[tuple[str, int]] = []

    async def snapshot(self, contract: str, limit: int = 30) -> dict:
        self.calls.append((contract, limit))
        return {"contract": contract, "requested_limit": limit, "data_status": "完整"}


def _deps(service: _MarketData) -> ResearchToolDeps:
    return ResearchToolDeps(
        provider=cast(ResearchDataProvider, object()),
        repo=cast(Repo, object()),
        mode="paper",
        market_data=service,
        watchlist_snapshot=("BTC_USDT", "ETH_USDT"),
    )


@pytest.mark.asyncio
async def test_market_tool_returns_snapshot_and_records_contract() -> None:
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
async def test_market_tool_rejects_contract_outside_run_snapshot() -> None:
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
    service = _MarketData()
    deps = _deps(service)

    result = await ResearchToolRegistry(deps).execute(
        "get_research_market_data", {"contract": "BTC_USDT", "limit": limit}
    )

    assert "limit" in result
    assert service.calls == []
