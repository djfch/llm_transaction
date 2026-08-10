"""研报 v2 逐标的 JSON 契约校验与安全归一化。"""

from __future__ import annotations

import json

_DIRECTIONS = frozenset(("偏多", "偏空", "中性"))
_CONFIDENCE = frozenset(("高", "中", "低"))
_REGIMES = frozenset(("上涨趋势", "下跌趋势", "震荡", "转折观察"))
_CONFIRMATIONS = frozenset(("确认", "冲突", "中性", "不可用"))
_BASIS_TYPES = frozenset(("事件驱动", "宏观驱动", "结构延续", "混合"))
_VIEW_FIELDS = (
    "contract",
    "direction",
    "confidence",
    "horizon",
    "market_regime",
    "technical_confirmation",
    "basis_type",
    "evidence",
    "risks",
    "narrative",
)


def _json_body(text: str) -> dict | None:
    body = text.strip()
    if body.startswith("```"):
        body = body.strip("`")
        if body.startswith("json"):
            body = body[4:]
    try:
        value = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _valid_evidence(value: object) -> bool:
    if not isinstance(value, list):
        return False
    return all(
        isinstance(item, dict)
        and isinstance(item.get("point"), str)
        and isinstance(item.get("source"), str)
        for item in value
    )


def _valid_view(view: object) -> bool:
    if not isinstance(view, dict) or not all(field in view for field in _VIEW_FIELDS):
        return False
    if not isinstance(view["contract"], str) or not view["contract"]:
        return False
    if view["direction"] not in _DIRECTIONS or view["confidence"] not in _CONFIDENCE:
        return False
    if view["market_regime"] not in _REGIMES:
        return False
    if view["technical_confirmation"] not in _CONFIRMATIONS:
        return False
    if view["basis_type"] not in _BASIS_TYPES:
        return False
    if not isinstance(view["horizon"], str) or not isinstance(view["narrative"], str):
        return False
    if not _valid_evidence(view["evidence"]):
        return False
    return isinstance(view["risks"], list) and all(isinstance(item, str) for item in view["risks"])


def parse_v2_payload(
    text: str,
    expected_contracts: tuple[str, ...],
    queried_contracts: set[str],
    data_statuses: dict[str, str],
) -> dict | None:
    """校验白名单、工具调用与逐标的结论集合完全一致。"""
    payload = _json_body(text)
    if payload is None:
        return None
    for field in ("summary", "cross_market_view", "global_risks", "asset_views"):
        if field not in payload:
            return None
    if not isinstance(payload["summary"], str) or not isinstance(payload["cross_market_view"], str):
        return None
    if not isinstance(payload["global_risks"], list) or not all(
        isinstance(item, str) for item in payload["global_risks"]
    ):
        return None
    views = payload["asset_views"]
    if not isinstance(views, list) or not all(_valid_view(view) for view in views):
        return None
    contracts = [view["contract"] for view in views]
    expected = set(expected_contracts)
    if len(contracts) != len(set(contracts)):
        return None
    if set(contracts) != expected or queried_contracts != expected:
        return None
    by_contract = {view["contract"]: view for view in views}
    adjustments: list[str] = []
    for contract in expected_contracts:
        view = by_contract[contract]
        if data_statuses.get(contract) == "不可用":
            view["direction"] = "中性"
            view["confidence"] = "低"
            view["technical_confirmation"] = "不可用"
            adjustments.append(f"{contract}: 行情不可用，结论归一为中性/低置信/技术不可用")
            continue
        if view["basis_type"] == "结构延续" and view["confidence"] == "高":
            view["confidence"] = "中"
            adjustments.append(f"{contract}: 结构延续结论由高置信降为中置信")
    payload["asset_views"] = [by_contract[contract] for contract in expected_contracts]
    payload["policy_adjustments"] = adjustments
    return payload
