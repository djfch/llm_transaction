"""研报 v2 最终 JSON：逐标的完整性与安全归一化。"""

from __future__ import annotations

import json

from src.research.payload import _parse_payload


def _view(contract: str, *, confidence: str = "中", basis_type: str = "结构延续") -> dict:
    """构造字段完整的单合约研报观点测试数据。

    参数：
        contract: str，观点对应的合约名称
        confidence: str，观点置信度，默认中
        basis_type: str，观点依据类型，默认结构延续

    返回：
        dict，可提交给研报载荷解析器的观点字典
    """
    return {
        "contract": contract,
        "direction": "偏多",
        "confidence": confidence,
        "horizon": "3日",
        "market_regime": "上涨趋势",
        "technical_confirmation": "确认",
        "basis_type": basis_type,
        "evidence": [{"point": "EMA向上", "source": "市场快照"}],
        "risks": ["波动放大"],
        "narrative": "日线结构向上。",
    }


def _payload(views: list[dict]) -> str:
    """把逐标的观点列表包装成研报 v2 JSON 文本。

    参数：
        views: list[dict]，逐合约观点字典列表

    返回：
        str，保留中文字符的完整研报 JSON 文本
    """
    return json.dumps(
        {
            "summary": "逐标的研判",
            "cross_market_view": "BTC 与 ETH 同步",
            "global_risks": ["宏观数据超预期"],
            "asset_views": views,
        },
        ensure_ascii=False,
    )


def test_v2_payload_requires_exact_whitelist_and_tool_coverage() -> None:
    """验证研报 v2 载荷仅在白名单与工具查询覆盖完全一致时通过。

    参数：无

    返回：
        None，通过断言验证解析成功且逐标的顺序与白名单一致
    """
    expected = ("BTC_USDT", "ETH_USDT")
    text = _payload([_view("BTC_USDT"), _view("ETH_USDT")])

    parsed = _parse_payload(text, expected_contracts=expected, queried_contracts=set(expected))

    assert parsed is not None
    assert [item["contract"] for item in parsed["asset_views"]] == list(expected)


def test_v2_payload_rejects_missing_duplicate_unknown_or_unqueried_contract() -> None:
    """验证研报 v2 拒绝缺失、重复、未知或未经查询的合约观点。

    参数：无

    返回：
        None，通过断言验证四类不完整覆盖均解析失败
    """
    expected = ("BTC_USDT", "ETH_USDT")

    assert (
        _parse_payload(
            _payload([_view("BTC_USDT")]),
            expected_contracts=expected,
            queried_contracts=set(expected),
        )
        is None
    )
    assert (
        _parse_payload(
            _payload([_view("BTC_USDT"), _view("BTC_USDT")]),
            expected_contracts=expected,
            queried_contracts=set(expected),
        )
        is None
    )
    assert (
        _parse_payload(
            _payload([_view("BTC_USDT"), _view("SOL_USDT")]),
            expected_contracts=expected,
            queried_contracts={"BTC_USDT", "SOL_USDT"},
        )
        is None
    )
    assert (
        _parse_payload(
            _payload([_view("BTC_USDT"), _view("ETH_USDT")]),
            expected_contracts=expected,
            queried_contracts={"BTC_USDT"},
        )
        is None
    )


def test_v2_payload_downgrades_high_confidence_structure_only_view() -> None:
    """验证仅由结构延续支撑的高置信观点会被降级为中置信。

    参数：无

    返回：
        None，通过断言验证置信度和策略调整说明同步更新
    """
    text = _payload([_view("BTC_USDT", confidence="高")])

    parsed = _parse_payload(text, expected_contracts=("BTC_USDT",), queried_contracts={"BTC_USDT"})

    assert parsed is not None
    assert parsed["asset_views"][0]["confidence"] == "中"
    assert parsed["policy_adjustments"] == ["BTC_USDT: 结构延续结论由高置信降为中置信"]


def test_v2_payload_normalizes_unavailable_market_view() -> None:
    """验证行情不可用时观点被安全归一为中性、低置信和技术不可用。

    参数：无

    返回：
        None，通过断言验证观点字段及策略调整说明
    """
    text = _payload([_view("BTC_USDT", confidence="高", basis_type="宏观驱动")])

    parsed = _parse_payload(
        text,
        expected_contracts=("BTC_USDT",),
        queried_contracts={"BTC_USDT"},
        data_statuses={"BTC_USDT": "不可用"},
    )

    assert parsed is not None
    view = parsed["asset_views"][0]
    assert (view["direction"], view["confidence"], view["technical_confirmation"]) == (
        "中性",
        "低",
        "不可用",
    )
    assert parsed["policy_adjustments"] == [
        "BTC_USDT: 行情不可用，结论归一为中性/低置信/技术不可用"
    ]


def test_v2_payload_rejects_invalid_asset_contract_fields() -> None:
    """验证研报 v2 拒绝非法技术确认枚举与错误的证据字段类型。

    参数：无

    返回：
        None，通过断言验证两类字段错误都导致解析失败
    """
    view = _view("BTC_USDT")
    view["technical_confirmation"] = "强烈确认"
    assert (
        _parse_payload(
            _payload([view]), expected_contracts=("BTC_USDT",), queried_contracts={"BTC_USDT"}
        )
        is None
    )

    view = _view("BTC_USDT")
    view["evidence"] = "不是列表"
    assert (
        _parse_payload(
            _payload([view]), expected_contracts=("BTC_USDT",), queried_contracts={"BTC_USDT"}
        )
        is None
    )
