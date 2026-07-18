"""手动验证脚本：实连 Gate 永续 WS，打印 30 秒 BTC_USDT ticker + 全周期 K 线订阅情况。

不进测试套件（需要真实网络）。用法：uv run python scripts/check_feed.py

用途：核对 GATE_CANDLE_INTERVALS 的 15 个周期在 futures.candlesticks 频道的真实
支持情况——订阅被拒时 feed 会打 warning（"K 线频道异常 ACK"），结束时按周期
统计收到的推送条数，条数为 0 且伴 warning 的周期即不受 WS 支持，应从列表剔除。
"""

import asyncio
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.gateway.base import Candle, Ticker  # noqa: E402
from src.market.feed import MarketFeed  # noqa: E402
from src.market.intervals import GATE_CANDLE_INTERVALS  # noqa: E402

DURATION = 30


def main() -> None:
    ticker_count = 0
    candle_counts: Counter[str] = Counter()

    def on_ticker(ticker: Ticker) -> None:
        nonlocal ticker_count
        ticker_count += 1
        print(
            f"[{time.strftime('%H:%M:%S')}] {ticker.contract} "
            f"last={ticker.last} mark={ticker.mark_price} "
            f"funding={ticker.funding_rate} 24h={ticker.change_percentage}%"
        )

    def on_candle(contract: str, interval: str, candle: Candle, closed: bool) -> None:
        candle_counts[interval] += 1

    async def run() -> None:
        feed = MarketFeed(
            ["BTC_USDT"],
            list(GATE_CANDLE_INTERVALS),
            on_ticker=on_ticker,
            on_candle=on_candle,
        )
        await feed.start()
        await asyncio.sleep(DURATION)
        await feed.stop()
        print(f"共收到 {ticker_count} 条 ticker 推送")
        print("各周期 K 线推送条数（0 条且伴 ACK warning 的周期不受 WS 支持）：")
        for interval in GATE_CANDLE_INTERVALS:
            print(f"  {interval}: {candle_counts[interval]}")

    asyncio.run(run())


if __name__ == "__main__":
    main()
