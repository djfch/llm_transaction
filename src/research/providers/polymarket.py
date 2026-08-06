"""Polymarket 预测市场：公开 Gamma API（无 key），httpx 跟随重定向。

借鉴 TradingAgents polymarket.py（Apache-2.0）：public-search + 过滤已关闭市场 +
按成交额排序 + 1 周变动；差异：异步 httpx。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx

from src.research.providers.base import ResearchSourceError

BASE_URL = "https://gamma-api.polymarket.com"
TIMEOUT = 30.0
DEFAULT_LIMIT = 6


class PolymarketSource:
    """Polymarket 预测概率源。"""

    def __init__(self, *, base_url: str = BASE_URL) -> None:
        self._base = base_url.rstrip("/")

    async def get_prediction_markets(self, topic: str, limit: int = DEFAULT_LIMIT) -> str:
        """返回主题匹配的未结算市场：隐含概率 + 成交额 + 结算日 + 1 周变动。"""
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
                resp = await client.get(
                    f"{self._base}/public-search",
                    params={"q": topic, "limit_per_type": 20},
                )
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPError as exc:
            raise ResearchSourceError(f"Polymarket 请求失败：{exc}") from exc
        now = datetime.now(timezone.utc)
        candidates = [
            m
            for event in data.get("events", [])
            for m in event.get("markets", [])
            if self._is_forward_looking(m, now)
        ]
        candidates.sort(key=lambda m: float(m.get("volumeNum") or 0), reverse=True)
        if not candidates:
            return f"## Polymarket: {topic!r}\n没有匹配的未结算市场。"
        lines = [f"## Polymarket 预测市场：{topic!r}", "（成交额越高越可靠，概率=市场定价非预测）"]
        for market in candidates[:limit]:
            prices = self._json_list(market.get("outcomePrices"))
            outcomes = self._json_list(market.get("outcomes"))
            if not prices or not outcomes:
                continue
            try:
                prob = float(prices[0])
            except (TypeError, ValueError):
                continue
            label = outcomes[0]
            volume = market.get("volumeNum") or 0
            end_date = str(market.get("endDate") or "")[:10]
            week = market.get("oneWeekPriceChange")
            week_txt = f"，1 周 {float(week) * 100:+.1f}pp" if _is_num(week) and week else ""
            lines.append(
                f"- **{market.get('question')}** — {label} {prob:.0%} "
                f"（成交 ${float(volume):,.0f}，结算 {end_date}{week_txt}）"
            )
        return "\n".join(lines)

    @staticmethod
    def _is_forward_looking(market: dict, now: datetime) -> bool:
        """只保留未结算且结算日在未来的市场（closed 才是可靠标志）。"""
        if market.get("closed"):
            return False
        end = market.get("endDate")
        if end:
            try:
                if datetime.fromisoformat(end.replace("Z", "+00:00")) < now:
                    return False
            except ValueError:
                pass
        return bool(PolymarketSource._json_list(market.get("outcomePrices"))) and bool(
            PolymarketSource._json_list(market.get("outcomes"))
        )

    @staticmethod
    def _json_list(value) -> list:
        """Gamma 把 outcomes/outcomePrices 编码为 JSON 字符串数组。"""
        if isinstance(value, list):
            return value
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return []


def _is_num(value) -> bool:
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False
