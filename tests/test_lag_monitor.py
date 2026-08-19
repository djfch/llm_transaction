"""事件循环 lag 监控测试：阻塞事件循环时产生告警日志，正常时静默。"""

from __future__ import annotations

import asyncio
import logging
import time

from src.lag_monitor import monitor_event_loop_lag


async def test_lag_monitor_warns_when_loop_blocked(caplog):
    """验证事件循环被同步阻塞占住时，监控协程记录 lag 告警日志。

    参数：
        caplog: LogCaptureFixture，pytest 日志捕获夹具

    返回：
        None，断言阻塞 0.3s（阈值 0.1s）后出现含"lag 告警"的 warning 日志
    """
    task = asyncio.create_task(monitor_event_loop_lag(interval_s=0.05, warn_threshold_s=0.1))
    try:
        with caplog.at_level(logging.WARNING, logger="src.lag_monitor"):
            await asyncio.sleep(0.12)  # 让监控先跑一两个周期（不应告警）
            time.sleep(0.3)  # 故意阻塞事件循环线程
            await asyncio.sleep(0.15)  # 放行后让监控完成测量并写日志
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert any("lag 告警" in record.message for record in caplog.records)


async def test_lag_monitor_silent_when_loop_healthy(caplog):
    """验证事件循环健康时监控协程不产生告警日志。

    参数：
        caplog: LogCaptureFixture，pytest 日志捕获夹具

    返回：
        None，断言健康运行若干周期后无任何"lag 告警"日志
    """
    task = asyncio.create_task(monitor_event_loop_lag(interval_s=0.05, warn_threshold_s=0.5))
    try:
        with caplog.at_level(logging.WARNING, logger="src.lag_monitor"):
            await asyncio.sleep(0.25)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert not any("lag 告警" in record.message for record in caplog.records)
