"""paper 模式 60 秒冒烟：Mock LLM + 模拟行情跑完整主程序，验证审计落库。

用法：uv run python scripts/smoke_paper.py
退出码 0 = 通过；断言失败抛异常非零退出。
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.audit.logger import setup_logging  # noqa: E402
from src.bootstrap import AppContext, build_app, run_app  # noqa: E402
from src.config import load_settings, load_watchlist  # noqa: E402
from src.gateway.base import Ticker  # noqa: E402
from src.market.candles import ManualPriceSource  # noqa: E402

DB_PATH = "data/smoke.db"


def _ticker(price: Decimal) -> Ticker:
    """按给定价格构造一笔 BTC_USDT 的模拟行情快照，用于推送给模拟行情源。

    参数：
        price: Decimal，最新成交价；标记价与 24h 高/低价同步取该值

    返回：
        Ticker：完整行情对象，资金费率固定 0.0001、24h 涨跌幅固定 0.5%
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


async def pusher(ctx: AppContext, duration: float) -> None:
    """按 duration 等比压缩原 60 秒节奏：开局与中期各手动唤醒一轮（短时长也 ≥2 轮决策）。

    参数：
        ctx: AppContext，风控规则上下文
        duration: float，冒烟运行总时长秒数
    返回：
        None，按 duration 等比压缩原 60 秒节奏：开局与中期各手动唤醒一轮（短时长也 ≥2 轮决策）
    """
    source = ctx.source
    assert isinstance(source, ManualPriceSource)
    scale = duration / 60.0
    price = Decimal("60000")
    await asyncio.sleep(3 * scale)
    ctx.scheduler.wake_now("smoke_manual_1")
    for i in range(26):
        price *= Decimal("1.0002")
        await source.push_ticker(_ticker(price))
        if i == 10:
            ctx.scheduler.wake_now("smoke_manual_2")
        await asyncio.sleep(2 * scale)


async def main() -> None:
    """冒烟入口：以 Mock LLM 与模拟行情跑完整主程序，结束后校验审计落库。

    参数：无

    返回：
        None，重建 data/smoke.db 并打印落库统计与 SMOKE PASS；运行时长取命令行
        第 1 个参数（秒），缺省 60 秒；断言失败时非零退出
    """
    Path(DB_PATH).unlink(missing_ok=True)
    settings = load_settings()
    settings.server.port = 0  # 随机空闲端口，避免占用
    settings.scheduler.autostart = (
        True  # 冒烟需要调度器响应手动唤醒（生产默认 false 由用户点击启动）
    )
    watchlist = load_watchlist()
    setup_logging(settings.log.dir, settings.log.level)
    ctx = await build_app(settings, watchlist, mock_llm=True, mock_market=True, db_path=DB_PATH)
    duration = float(sys.argv[1]) if len(sys.argv) > 1 else 60.0
    await run_app(ctx, duration=duration, price_pusher=lambda c: pusher(c, duration))

    conn = sqlite3.connect(DB_PATH)
    counts = {
        name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
        for name in ("decisions", "audit_rounds", "audit_tool_calls", "notes")
    }
    print(f"冒烟落库统计：{counts}")
    assert counts["decisions"] >= 2, "应至少有 2 轮决策"
    assert counts["audit_rounds"] >= 2, "应至少有 2 条审计主表记录"
    assert counts["audit_tool_calls"] >= 3, "首轮应有 3 次工具调用落审计"
    assert counts["notes"] >= 1, "应有 LLM 笔记"
    row = conn.execute(
        "SELECT prompt_snapshot, llm_raw FROM audit_rounds ORDER BY started_at LIMIT 1"
    ).fetchone()
    assert row and row[0] and row[1], "审计应含 prompt 快照与 LLM 原始输出"
    print("SMOKE PASS")


if __name__ == "__main__":
    asyncio.run(main())
