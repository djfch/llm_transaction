"""主入口：加载配置 → 组装应用 → 运行（Ctrl+C 优雅退出）。

用法：
    uv run python -m src.main                # 按 config.yaml 的 mode 运行（默认 paper）
    LLM_MOCK=1 uv run python -m src.main     # 使用 Mock LLM（联调链路用）
"""

from __future__ import annotations

import asyncio

from src.audit.logger import setup_logging
from src.bootstrap import build_app, run_app
from src.config import load_settings, load_watchlist


async def _main() -> None:
    settings = load_settings()
    watchlist = load_watchlist()
    setup_logging(settings.log.dir, settings.log.level)
    ctx = await build_app(settings, watchlist)
    await run_app(ctx)


def main() -> None:
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass  # Ctrl+C：run_app 的 finally 已完成优雅退出


if __name__ == "__main__":
    main()
