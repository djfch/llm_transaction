"""研报 mock provider 必须遵守逐标的 v2 工具与输出契约。"""

from __future__ import annotations

import json

import pytest

from src.research.mock_provider import ResearchMockProvider


@pytest.mark.asyncio
async def test_mock_provider_queries_every_watchlist_contract_then_returns_v2_payload() -> None:
    """校验研报 mock provider 先逐标的查询市场数据、再产出合法 v2 研报 JSON 的完整契约。

    参数：无

    返回：
        None，断言首轮对白名单中的 BTC_USDT、ETH_USDT 各恰好发起一次
        get_research_market_data 调用（limit=30 且顺序与白名单一致）；
        回填工具结果后次轮返回 schema_version=3 的研报 JSON，
        asset_views 按序覆盖两个合约，且各标的 basis_type 为「结构延续」、confidence 为「低」
    """
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
    assert payload["schema_version"] == 3
    assert [view["contract"] for view in payload["asset_views"]] == [
        "BTC_USDT",
        "ETH_USDT",
    ]
    assert all(view["basis_type"] == "结构延续" for view in payload["asset_views"])
    assert all(view["confidence"] == "低" for view in payload["asset_views"])
