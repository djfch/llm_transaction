"""主入口：加载 .env 与配置 → 组装应用 → 运行（Ctrl+C 优雅退出）。

用法：
    uv run python -m src.main                # 按 config.yaml 的 mode 运行（默认 paper）
    LLM_MOCK=1 uv run python -m src.main     # 使用 Mock LLM（联调链路用）
"""

from __future__ import annotations

import asyncio

from dotenv import load_dotenv

from src.audit.logger import setup_logging
from src.bootstrap import build_app, run_app
from src.config import load_settings, load_watchlist


async def _main() -> None:
    """按启动流程初始化并运行整个应用：加载密钥与配置、初始化日志、组装并启动各模块。

    参数：无

    返回：
        None，副作用是启动应用主循环（行情、风控、Agent、调度、通知、监控等），
        一直运行到收到退出信号为止
    """
    load_dotenv()  # 启动时加载 .env（交易所/LLM key）；已存在的环境变量优先
    settings = load_settings()
    watchlist = load_watchlist()
    setup_logging(settings.log.dir, settings.log.level)
    ctx = await build_app(settings, watchlist)
    await run_app(ctx)


def main() -> None:
    """程序同步入口：启动 asyncio 事件循环运行主流程，Ctrl+C 时安静退出。

    参数：无

    返回：
        None，副作用是运行整个交易应用；Ctrl+C 的优雅退出由 run_app 的 finally 完成，
        此处仅吞掉 KeyboardInterrupt 避免打印堆栈
    """
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass  # Ctrl+C：run_app 的 finally 已完成优雅退出


if __name__ == "__main__":
    main()
