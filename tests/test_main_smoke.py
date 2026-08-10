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
    """构造一条 BTC_USDT 的模拟行情快照，价格外字段使用固定值。

    参数：
        price: Decimal，最新价与标记价使用同一价格

    返回：
        Ticker：可直接推入模拟行情源的行情对象
    """
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
    """冒烟测试的行情推送回调：先唤醒调度器，再以微涨节奏推 6 条行情。

    参数：
        ctx: AppContext，已组装好的应用上下文，从中取调度器与模拟行情源

    返回：
        None，副作用：触发一次调度唤醒并向行情源推入 6 条 BTC_USDT 行情
    """
    await asyncio.sleep(2)
    ctx.scheduler.wake_now("test_wake")
    price = Decimal("60000")
    for _ in range(6):
        price *= Decimal("1.0001")
        await ctx.source.push_ticker(_ticker(price))  # type: ignore[attr-defined]
        await asyncio.sleep(1)


async def test_paper_smoke(tmp_path: Path):
    """主程序全链路冒烟：Mock LLM + 模拟行情跑 12 秒后，审计表落库完整。

    参数：
        tmp_path: Path，pytest 临时目录夹具，冒烟数据库文件落在其中

    返回：
        None，断言至少 1 轮决策、首轮至少 3 次工具调用落审计，
        且审计记录含 prompt 快照与 LLM 原始输出可完整溯源
    """
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
    """autostart=False（生产默认）：run_app 不启动调度器，无任何决策轮。

    参数：
        tmp_path: Path，pytest 提供的临时目录夹具

    返回：
        None：断言关闭自动启动后不会产生决策轮
    """
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
