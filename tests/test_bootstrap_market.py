"""行情链防护与 K 线回补接线回归测试（P1-#3、P1-#10、P3-#26）。

覆盖：
- bootstrap on_ticker 总闸：paper 撮合与触发器检查的异常各记各的日志，不外抛进 WS 任务
- MarketFeed 回调与解析异常：记日志不传播（防 gatews 任务 exception never retrieved）
- 启动时 REST 回补历史 K 线；paper 模式注入 candle_provider；回补失败降级为告警
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from decimal import Decimal
from pathlib import Path

import pytest

from src.bootstrap import AppContext, build_app
from src.config import Settings, Watchlist
from src.gateway.base import Candle, Ticker
from src.market.feed import MarketFeed

BTC = "BTC_USDT"


def _ticker(price: Decimal) -> Ticker:
    return Ticker(
        contract=BTC,
        last=price,
        mark_price=price,
        funding_rate=Decimal("0.0001"),
        high_24h=price,
        low_24h=price,
        change_percentage=Decimal("0.5"),
    )


class _Response:
    """最小 WebSocketResponse 替身（本层只用到 error/event/result 三字段）。"""

    def __init__(self, result: list[dict]) -> None:
        self.error = None
        self.event = "update"
        self.result = result


def _ticker_raw(last: str = "60000") -> dict:
    return {
        "contract": BTC,
        "last": last,
        "mark_price": last,
        "funding_rate": "0.0001",
        "high_24h": last,
        "low_24h": last,
        "change_percentage": "0.5",
    }


def _candle_raw(t: int = 3600) -> dict:
    return {"n": f"1h_{BTC}", "t": t, "o": "1", "h": "1", "l": "1", "c": "1", "v": "1", "w": True}


@pytest.fixture
async def build_ctx(tmp_path: Path) -> AsyncIterator[Callable[..., AppContext]]:
    """build_app 工厂 + 统一清理（关闭数据库，避免 aiosqlite 线程跨用例泄漏）。"""
    ctxs: list[AppContext] = []

    async def _factory(**kwargs) -> AppContext:
        ctx = await build_app(
            Settings(),
            Watchlist(contracts=[BTC]),
            mock_llm=True,
            mock_market=True,
            db_path=tmp_path / "t.db",
            **kwargs,
        )
        ctxs.append(ctx)
        return ctx

    yield _factory
    for ctx in ctxs:
        await ctx.db.close()


# ---------- bootstrap on_ticker 总闸（P1-#3） ----------


async def test_on_ticker_match_error_logged_not_raised(build_ctx, caplog):
    """paper 撮合异常：记日志不外抛，触发器检查仍正常执行。"""
    ctx = await build_ctx()

    def boom(*args, **kwargs):
        raise RuntimeError("撮合故障")

    ctx.gateway.on_price = boom  # type: ignore[method-assign]
    ctx.triggers.add(BTC, ">=", Decimal("60000"))
    with caplog.at_level(logging.ERROR, logger="src.bootstrap"):
        await ctx.source.push_ticker(_ticker(Decimal("60000")))  # type: ignore[attr-defined]
    assert any("撮合异常" in r.message for r in caplog.records)
    assert ctx.triggers.list(BTC) == []  # 触发器检查未被撮合异常拖垮（已触发并失效）


async def test_on_ticker_trigger_error_logged_not_raised(build_ctx, caplog):
    """触发器检查异常：记日志不外抛，paper 撮合已先正常执行。"""
    ctx = await build_ctx()

    def boom(*args, **kwargs):
        raise RuntimeError("触发器故障")

    ctx.triggers.check = boom  # type: ignore[method-assign]
    with caplog.at_level(logging.ERROR, logger="src.bootstrap"):
        await ctx.source.push_ticker(_ticker(Decimal("60000")))  # type: ignore[attr-defined]
    assert any("触发器检查异常" in r.message for r in caplog.records)
    assert len(ctx.gateway.get_tickers()) == 1  # 行情快照已写入（撮合不受影响）


# ---------- MarketFeed 回调防护（P3-#26） ----------


async def test_feed_ticker_callback_error_logged_not_raised(caplog):
    """ticker 回调抛错：记日志不传播（不进 gatews 任务）。"""
    feed = MarketFeed([BTC], ["1h"])

    def boom(ticker: Ticker) -> None:
        raise RuntimeError("回调故障")

    feed.set_handlers(on_ticker=boom)
    with caplog.at_level(logging.ERROR, logger="src.market.feed"):
        await feed._handle_ticker(None, _Response([_ticker_raw()]))  # type: ignore[arg-type]
    assert any("ticker 回调异常" in r.message for r in caplog.records)


async def test_feed_bad_ticker_skipped_good_one_delivered(caplog):
    """单条解析失败跳过并记日志，同批正常条目仍送达回调。"""
    received: list[Ticker] = []
    feed = MarketFeed([BTC], ["1h"])
    feed.set_handlers(on_ticker=received.append)
    with caplog.at_level(logging.ERROR, logger="src.market.feed"):
        await feed._handle_ticker(  # type: ignore[arg-type]
            None, _Response([{"bad": "payload"}, _ticker_raw()])
        )
    assert len(received) == 1
    assert received[0].contract == BTC
    assert any("解析失败" in r.message for r in caplog.records)


async def test_feed_candle_callback_error_logged_not_raised(caplog):
    """K 线回调抛错：记日志不传播。"""
    feed = MarketFeed([BTC], ["1h"])

    def boom(*args) -> None:
        raise RuntimeError("回调故障")

    feed.set_handlers(on_candle=boom)
    with caplog.at_level(logging.ERROR, logger="src.market.feed"):
        await feed._handle_candle(None, _Response([_candle_raw()]))  # type: ignore[arg-type]
    assert any("K 线回调异常" in r.message for r in caplog.records)


# ---------- K 线启动回补（P1-#10） ----------


def _fake_provider(contract, interval, limit, from_ts, to_ts):
    """假 K 线 provider：10 根 1h 历史（与 PaperGateway 的位置参数调用签名一致）。"""
    return [
        Candle(t=3600 * i, o=Decimal(i), h=Decimal(i), l=Decimal(i), c=Decimal(i), v=Decimal(1))
        for i in range(1, 11)
    ]


async def test_build_app_backfills_candles_with_injected_provider(build_ctx):
    """启动即用注入 provider 回补历史 K 线；paper 网关也拿到同一 provider。"""
    ctx = await build_ctx(candle_provider=_fake_provider)
    recent = ctx.candles.get_recent(BTC, "1h", 200)
    assert [c.t for c in recent] == [3600 * i for i in range(1, 11)]
    assert len(ctx.gateway.get_candlesticks(BTC, "1h", 5)) == 10  # provider 已注入 paper 网关


async def test_build_app_backfill_failure_degrades_to_warning(build_ctx, caplog):
    """回补失败降级为告警日志，不阻断启动（WS 仍会逐根积累）。"""

    def boom(*args):
        raise RuntimeError("REST 不可用")

    with caplog.at_level(logging.WARNING, logger="src.bootstrap"):
        ctx = await build_ctx(candle_provider=boom)
    assert ctx.candles.get_recent(BTC, "1h", 5) == []
    assert any("回补失败" in r.message for r in caplog.records)


async def test_mock_market_without_provider_skips_backfill(build_ctx):
    """mock 行情且无 provider：完全不做回补（无网络依赖，冒烟可离线跑）。"""
    ctx = await build_ctx()
    assert ctx.candles.get_recent(BTC, "1h", 5) == []
    assert ctx.gateway.get_candlesticks(BTC, "1h", 5) == []  # 未注入 provider
