"""行情链防护与 K 线回补接线测试。

覆盖：
- bootstrap on_ticker 总闸：paper 撮合与触发器检查的异常各记各的日志，不外抛进 WS 任务
- MarketFeed 回调与解析异常：记日志不传播（防 gatews 任务 exception never retrieved）
- 启动时 REST 回补历史 K 线；paper 模式注入 candle_provider；回补失败降级为告警
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from decimal import Decimal
from pathlib import Path

import pytest

from src.agent.ticker_fanout import make_on_ticker
from src.bootstrap import AppContext, build_app
from src.config import Settings, Watchlist
from src.gateway.base import Candle, Ticker
from src.market.feed import MarketFeed

BTC = "BTC_USDT"


def test_interval_seconds():
    """周期字符串转秒数：覆盖各时间单位与非法周期拒绝。"""
    from src.market.intervals import interval_seconds

    assert interval_seconds("10s") == 10
    assert interval_seconds("30m") == 1800
    assert interval_seconds("4h") == 14400
    assert interval_seconds("1d") == 86400
    assert interval_seconds("7d") == 604800
    assert interval_seconds("30d") == 2592000
    assert interval_seconds("1w") == 604800
    with pytest.raises(ValueError, match="非法 K 线周期"):
        interval_seconds("3h")


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


# ---------- bootstrap on_ticker 总闸 ----------


async def test_on_ticker_match_error_logged_not_raised(build_ctx, caplog):
    """paper 撮合异常：记日志不外抛，触发器检查仍正常执行。"""
    ctx = await build_ctx()

    def boom(*args, **kwargs):
        raise RuntimeError("撮合故障")

    ctx.gateway.on_price = boom  # type: ignore[method-assign]
    ctx.triggers.add(BTC, ">=", Decimal("60000"))
    with caplog.at_level(logging.ERROR, logger="src.agent.ticker_fanout"):
        await ctx.source.push_ticker(_ticker(Decimal("60000")))  # type: ignore[attr-defined]
    assert any("撮合异常" in r.message for r in caplog.records)
    assert ctx.triggers.list(BTC) == []  # 触发器检查未被撮合异常拖垮（已触发并失效）


async def test_on_ticker_trigger_error_logged_not_raised(build_ctx, caplog):
    """触发器检查异常：记日志不外抛，paper 撮合已先正常执行。"""
    ctx = await build_ctx()

    def boom(*args, **kwargs):
        raise RuntimeError("触发器故障")

    ctx.triggers.check = boom  # type: ignore[method-assign]
    with caplog.at_level(logging.ERROR, logger="src.agent.ticker_fanout"):
        await ctx.source.push_ticker(_ticker(Decimal("60000")))  # type: ignore[attr-defined]
    assert any("触发器检查异常" in r.message for r in caplog.records)
    assert len(ctx.gateway.get_tickers()) == 1  # 行情快照已写入（撮合不受影响）


# ---------- MarketFeed 回调防护 ----------


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


async def test_feed_error_ack_logged_not_silent(caplog):
    """订阅/推送 ACK 报错时记录 warning，确保订阅拒绝可观测。"""
    feed = MarketFeed([BTC], ["1h"])
    resp = _Response([])
    resp.error = {"message": "invalid interval"}  # type: ignore[assignment]
    with caplog.at_level(logging.WARNING, logger="src.market.feed"):
        await feed._handle_ticker(None, resp)  # type: ignore[arg-type]
        await feed._handle_candle(None, resp)  # type: ignore[arg-type]
    assert sum("ACK" in r.message for r in caplog.records) == 2


# ---------- K 线启动回补 ----------


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
    # 全周期回补：LLM 可查询 GATE_CANDLE_INTERVALS 中的任意周期
    assert ctx.candles.get_recent(BTC, "4h", 5)
    assert ctx.candles.get_recent(BTC, "1w", 5)


async def test_backfill_single_interval_failure_isolated(build_ctx, caplog):
    """单个周期回补失败不影响其他周期。"""

    def only_10s_fails(contract, interval, limit, from_ts, to_ts):
        if interval == "10s":
            raise RuntimeError("10s 不可用")
        return _fake_provider(contract, interval, limit, from_ts, to_ts)

    with caplog.at_level(logging.WARNING, logger="src.market.candles"):
        ctx = await build_ctx(candle_provider=only_10s_fails)
    assert ctx.candles.get_recent(BTC, "10s", 5) == []  # 失败周期为空
    assert ctx.candles.get_recent(BTC, "1h", 5)  # 其余周期照常回补
    assert ctx.candles.get_recent(BTC, "4h", 5)
    assert any("回补失败" in r.message for r in caplog.records)


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


# ---------- ticker WS 广播（前端实时价） ----------


async def test_on_ticker_broadcast_throttled_per_contract(build_ctx):
    """同合约节流窗口内连推两条：只广播首条；载荷对齐前端契约且 last 为 float。"""
    ctx = await build_ctx()
    await ctx.source.push_ticker(_ticker(Decimal("60000")))  # type: ignore[attr-defined]
    await ctx.source.push_ticker(_ticker(Decimal("60001")))  # type: ignore[attr-defined]

    evt = ctx.event_queue.get_nowait()
    assert evt == {"type": "ticker", "data": {"contract": BTC, "last": 60000.0}}
    assert isinstance(evt["data"]["last"], float)  # Decimal 会让 ws send_json 序列化崩
    with pytest.raises(asyncio.QueueEmpty):
        ctx.event_queue.get_nowait()  # 第二条被节流，队列无残余


async def test_on_ticker_broadcast_interval_zero_sends_all(build_ctx):
    """broadcast_interval=0 关闭节流：逐条广播（验证节流窗口语义本身生效）。"""
    ctx = await build_ctx()
    sent: list[dict] = []
    handler = make_on_ticker(ctx.gateway, ctx.triggers, sent.append, broadcast_interval=0)
    handler(_ticker(Decimal("1")))
    handler(_ticker(Decimal("2")))
    assert [e["data"]["last"] for e in sent] == [1.0, 2.0]


async def test_on_ticker_broadcast_error_logged_not_raised(build_ctx, caplog):
    """广播异常：记日志不外抛（与撮合/触发器防护同级，护住 WS 任务）。"""
    ctx = await build_ctx()

    def boom(msg: dict) -> None:
        raise RuntimeError("广播故障")

    handler = make_on_ticker(ctx.gateway, ctx.triggers, boom)
    with caplog.at_level(logging.ERROR, logger="src.agent.ticker_fanout"):
        handler(_ticker(Decimal("60000")))
    assert any("广播异常" in r.message for r in caplog.records)


# ---------- K 线周期单一数据源 ----------


def test_candle_intervals_single_source():
    """LLM 工具可请求周期 ⊆ 缓存订阅回补周期，且三处引用同一常量（防漂移）。"""
    from src.agent import tool_schemas
    from src.bootstrap import CANDLE_INTERVALS
    from src.market.intervals import GATE_CANDLE_INTERVALS

    assert CANDLE_INTERVALS == list(GATE_CANDLE_INTERVALS)
    assert tool_schemas._INTERVALS == list(GATE_CANDLE_INTERVALS)
    assert set(tool_schemas._INTERVALS) <= set(CANDLE_INTERVALS)
