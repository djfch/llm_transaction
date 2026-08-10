"""研报 mock provider 必须遵守逐标的 v2 工具与输出契约。"""

from __future__ import annotations

import json

import pytest

from src.research.mock_provider import ResearchMockProvider


@pytest.mark.asyncio
async def test_mock_provider_queries_every_watchlist_contract_then_returns_v2_payload() -> None:
    provider = ResearchMockProvider()
    messages = [
        {
            "role": "user",
            "content": (
                "## 本轮白名单（已冻结）\n"
                "- BTC_USDT\n"
                "- ETH_USDT\n"
                "必须对以上每个合约恰好调用一次 get_research_market_data。"
            ),
        }
    ]

    first = await provider.chat("system", messages, [])

    market_calls = [call for call in first.tool_calls if call.name == "get_research_market_data"]
    assert [call.args for call in market_calls] == [
        {"contract": "BTC_USDT", "limit": 30},
        {"contract": "ETH_USDT", "limit": 30},
    ]

    messages.append(first.assistant_message)
    for call in first.tool_calls:
        messages.append(provider.tool_result_message(call, "{}"))
    second = await provider.chat("system", messages, [])
    payload = json.loads(second.text)
    assert payload["schema_version"] == 2
    assert [view["contract"] for view in payload["asset_views"]] == [
        "BTC_USDT",
        "ETH_USDT",
    ]
    assert all(view["basis_type"] == "结构延续" for view in payload["asset_views"])
    assert all(view["confidence"] == "低" for view in payload["asset_views"])
