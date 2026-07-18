"""跨层共享的小工具：无业务依赖，任何层都可导入（避免高层反向依赖低层）。"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable


async def maybe_await(result: Awaitable[None] | None) -> None:
    """处理函数允许同步或协程，统一在此消化。"""
    if inspect.isawaitable(result):
        await result
