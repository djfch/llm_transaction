"""手动验证脚本：实连 Gate 永续 WS，打印 30 秒 BTC_USDT ticker。

不进测试套件（需要真实网络）。用法：uv run python scripts/check_feed.py
"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.gateway.base import Ticker  # noqa: E402
from src.market.feed import MarketFeed  # noqa: E402

DURATION = 30


def main() -> None:
    count = 0

    def on_ticker(ticker: Ticker) -> None:
        nonlocal count
        count += 1
        print(
            f"[{time.strftime('%H:%M:%S')}] {ticker.contract} "
            f"last={ticker.last} mark={ticker.mark_price} "
            f"funding={ticker.funding_rate} 24h={ticker.change_percentage}%"
        )

    async def run() -> None:
        feed = MarketFeed(["BTC_USDT"], [], on_ticker=on_ticker)
        await feed.start()
        await asyncio.sleep(DURATION)
        await feed.stop()
        print(f"共收到 {count} 条 ticker 推送")

    asyncio.run(run())


if __name__ == "__main__":
    main()
