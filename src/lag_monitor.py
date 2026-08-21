"""事件循环 lag 监控：周期测量调度延迟，超阈值记告警日志（issue #72 建议 5）。

原理：asyncio.sleep(interval) 的实际苏醒时刻晚于预期时刻的差值即事件循环
调度延迟（lag）；同步阻塞调用占住事件循环线程时 lag 显著抬升。
网关 I/O 全部经统一卸载层后，lag 告警是"有人绕过卸载层/其他阻塞回归"的哨兵。
"""

from __future__ import annotations

import asyncio

from src.audit.logger import get_logger

logger = get_logger(__name__)

_INTERVAL_S = 1.0  # 默认测量周期
_WARN_THRESHOLD_S = 2.0  # 默认告警阈值：调度延迟超过它说明事件循环疑似被阻塞


async def monitor_event_loop_lag(
    *, interval_s: float = _INTERVAL_S, warn_threshold_s: float = _WARN_THRESHOLD_S
) -> None:
    """永久协程：每 interval_s 测量一次调度延迟，超阈值记 warning 日志。

    随任务取消退出（CancelledError 自然上抛，由装配层负责取消）。

    参数：
        interval_s: float，测量周期秒数；省略时默认 1.0
        warn_threshold_s: float，告警阈值秒数；省略时默认 2.0

    返回：
        None，协程随取消结束；副作用为超阈值时写 warning 日志
    """
    loop = asyncio.get_running_loop()
    expected = loop.time() + interval_s
    while True:
        await asyncio.sleep(interval_s)
        now = loop.time()
        lag = now - expected
        if lag > warn_threshold_s:
            logger.warning(
                "事件循环 lag 告警：调度延迟 %.2fs（阈值 %.2fs），疑似存在未卸载的阻塞调用",
                lag,
                warn_threshold_s,
            )
        expected = now + interval_s
