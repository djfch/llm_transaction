"""主程序接线冒烟测试：Mock LLM + 模拟行情跑 12 秒，验证全链路落库。"""

from __future__ import annotations

import asyncio
import sqlite3
from decimal import Decimal
from pathlib import Path

from src.bootstrap import AppContext, build_app, run_app
from src.config import load_settings, load_watchlist
from src.gateway.base import Ticker


def _ticker(price: Decimal) -> Ticker:
    return Ticker(
        contract="BTC_USDT",
        last=price,
        mark_price=price,
        funding_rate=Decimal("0.0001"),
        high_24h=price,
        low_24h=price,
        change_percentage=Decimal("0.5"),
    )


async def _pusher(ctx: AppContext) -> None:
    await asyncio.sleep(2)
    ctx.scheduler.wake_now("test_wake")
    price = Decimal("60000")
    for _ in range(6):
        price *= Decimal("1.0001")
        await ctx.source.push_ticker(_ticker(price))  # type: ignore[attr-defined]
        await asyncio.sleep(1)


async def test_paper_smoke(tmp_path: Path):
    db_path = tmp_path / "smoke.db"
    settings = load_settings()
    settings.server.port = 0
    settings.scheduler.autostart = (
        True  # 测试需调度器响应 wake_now（生产默认 false 由用户点击启动）
    )
    ctx = await build_app(
        settings, load_watchlist(), mock_llm=True, mock_market=True, db_path=db_path
    )
    await run_app(ctx, duration=12, price_pusher=_pusher)

    conn = sqlite3.connect(db_path)
    rounds = conn.execute("SELECT COUNT(*) FROM audit_rounds").fetchone()[0]
    calls = conn.execute("SELECT COUNT(*) FROM audit_tool_calls").fetchone()[0]
    assert rounds >= 1, "至少 1 轮决策落审计"
    assert calls >= 3, "首轮 3 次工具调用落审计"
    row = conn.execute("SELECT prompt_snapshot, llm_raw FROM audit_rounds LIMIT 1").fetchone()
    assert row and row[0] and row[1], "审计可完整溯源（prompt + LLM 原始输出）"


async def test_run_app_no_autostart(tmp_path: Path):
    """autostart=False（生产默认）：run_app 不启动调度器，无任何决策轮。"""
    db_path = tmp_path / "smoke.db"
    settings = load_settings()
    settings.server.port = 0
    assert settings.scheduler.autostart is False  # 默认值契约：由用户点击启动
    ctx = await build_app(
        settings, load_watchlist(), mock_llm=True, mock_market=True, db_path=db_path
    )
    await run_app(ctx, duration=3)

    assert ctx.scheduler.is_running is False
    conn = sqlite3.connect(db_path)
    rounds = conn.execute("SELECT COUNT(*) FROM audit_rounds").fetchone()[0]
    assert rounds == 0, "未点击启动时不应有任何决策轮"
