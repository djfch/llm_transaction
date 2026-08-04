"""前后端跨层契约测试：真实 FastAPI 路由 + 真实 IndicatorService（假 K 线/OI 缓存）。

锁定「前端参数 → 路由校验 → 指标服务 → 响应形状」契约（Codex 评审 P1/P2 回归）：
- 前端 KlinePanel 四个页面周期的默认 limit（INTERVAL_LIMIT）一律不被 422；
- 完整短名单（含 scalar）作为 keys 时：atr14 有序列、oi 有 current，徽标不会"无数据"；
- 序列与最后 limit 根 K 线时间逐点对齐。
"""

from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from src.gateway.base import Candle
from src.market.indicator_service import IndicatorService
from src.memory.db import Database
from src.memory.repo import Repo
from src.server.app import create_app
from src.server.deps import ServerDeps

BTC = "BTC_USDT"

# 前端四个页面周期的默认窗口（web/src/components/console/KlinePanel.tsx INTERVAL_LIMIT）
PAGE_LIMITS = {"15m": 700, "1h": 200, "4h": 100, "1d": 60}
SHORTLIST = ["ema20", "ema50", "rsi14", "macd", "atr14", "oi"]  # 后端默认短名单基线


class _FakeCandleCache:
    """内存 K 线缓存（与 CandleCache.get_recent 同签名）：各周期 800 根单调上行 1h 粒度假 K 线。"""

    def __init__(self) -> None:
        self._bars = {
            interval: [
                Candle(
                    t=1_700_000_000 + i * 3600,
                    o=Decimal(100 + i),
                    h=Decimal(101 + i),
                    l=Decimal(99 + i),
                    c=Decimal(100 + i),
                    v=Decimal(10),
                )
                for i in range(800)
            ]
            for interval in PAGE_LIMITS
        }

    def get_recent(self, contract: str, interval: str, n: int) -> list[Candle]:
        return list(self._bars.get(interval, []))[-n:]


class _FakeOiCache:
    def get(self, contract: str) -> Decimal | None:
        return Decimal("123456")


@pytest.fixture
async def repo(tmp_path: Path) -> AsyncIterator[Repo]:
    db = Database()
    await db.open(tmp_path / "t.db")
    yield Repo(db)
    await db.close()


def _real_bundle() -> SimpleNamespace:
    """真实 IndicatorService 驱动的回调束（只实现 series 查询面，够本测试用）。"""
    service = IndicatorService(_FakeCandleCache(), _FakeOiCache())
    return SimpleNamespace(
        series=lambda contract, interval, keys, limit: service.series(
            contract, interval, keys or SHORTLIST, limit
        )
    )


def _client(repo: Repo, tmp_path: Path) -> AsyncClient:
    deps = ServerDeps(
        repo=repo,
        runtime_watchlist=[BTC],
        web_dist=tmp_path / "no_dist",
        indicators=_real_bundle(),
    )
    return AsyncClient(transport=ASGITransport(app=create_app(deps)), base_url="http://test")


@pytest.mark.parametrize("interval,limit", list(PAGE_LIMITS.items()))
async def test_frontend_default_page_limits_accepted(repo: Repo, tmp_path: Path, interval, limit):
    """契约：前端各周期默认 limit（700/200/100/60）经真实路由与服务返回 200 且逐点对齐。"""
    async with _client(repo, tmp_path) as c:
        r = await c.get(
            f"/api/indicators/series?contract={BTC}&interval={interval}"
            f"&limit={limit}&keys={','.join(SHORTLIST)}"
        )
    assert r.status_code == 200, f"{interval} limit={limit} 被拒: {r.text}"
    body = r.json()
    assert body["contract"] == BTC and body["interval"] == interval
    series = body["series"]
    # 完整短名单各项都有条目（scalar 不缺：徽标有数据）
    assert set(series) == set(SHORTLIST)
    # overlay/pane 序列与最后 limit 根 K 线逐点对齐（暖机覆盖，首点不为 null）
    ema20 = series["ema20"]["fields"]["ema20"]
    assert len(ema20) == limit and ema20[0]["value"] is not None
    cache_times = [bar.t for bar in _FakeCandleCache().get_recent(BTC, interval, limit)]
    assert [p["time"] for p in ema20] == cache_times
    # macd 多子字段；atr14（scalar）有序列；oi 无序列只有 current
    assert set(series["macd"]["fields"]) == {"dif", "dea", "hist"}
    assert len(series["atr14"]["fields"]["atr14"]) == limit
    assert series["oi"]["fields"] == {} and series["oi"]["current"] == "123456"


async def test_series_default_keys_uses_shortlist(repo: Repo, tmp_path: Path):
    """契约：keys 缺省时后端用当前短名单（含 scalar），等价于前端显式传完整短名单。"""
    async with _client(repo, tmp_path) as c:
        r = await c.get(f"/api/indicators/series?contract={BTC}&interval=1h&limit=200")
    assert r.status_code == 200
    assert set(r.json()["series"]) == set(SHORTLIST)
