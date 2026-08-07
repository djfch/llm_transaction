"""研报最终输出的 JSON 解析：容错剥离 Markdown 代码块 + 必填字段/取值校验。

从 agent.py 拆出（单文件行数门禁：agent.py 超 300 行软上限）；纯函数，供 agent 与测试共用。
"""

from __future__ import annotations

import json

_REQUIRED_FIELDS = ("direction", "confidence")
_VALID_DIRECTIONS = ("偏多", "偏空", "中性")
_VALID_CONFIDENCE = ("高", "中", "低")


def _parse_payload(text: str) -> dict | None:
    """容错解析研报 JSON：剥离可能的 Markdown 代码块包裹。"""
    body = text.strip()
    if body.startswith("```"):
        body = body.strip("`")
        if body.startswith("json"):
            body = body[4:]
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    if not all(field in payload for field in _REQUIRED_FIELDS):
        return None
    if payload["direction"] not in _VALID_DIRECTIONS:
        return None
    if payload["confidence"] not in _VALID_CONFIDENCE:
        return None
    # L6：evidence/risks 必须为列表（LLM 输出字符串时不照存，触发重试规范化）
    for field in ("evidence", "risks"):
        if field in payload and not isinstance(payload[field], list):
            return None
    return payload
