"""研报最终输出解析：只接受逐标的协议。"""

from __future__ import annotations

from src.research.payload_v2 import parse_v2_payload


def _parse_payload(
    text: str,
    *,
    expected_contracts: tuple[str, ...],
    queried_contracts: set[str],
    data_statuses: dict[str, str] | None = None,
) -> dict | None:
    """解析并校验白名单、市场工具调用与逐标的结论三集合一致。"""
    return parse_v2_payload(text, expected_contracts, queried_contracts, data_statuses or {})
