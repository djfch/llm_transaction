"""复盘调度：每日定时触发 + 手动触发，asyncio.Lock 防重入（设计 spec §2）。

- run_forever：每 60s 巡检一次（镜像 bootstrap._funding_loop），到点且当日未复盘
  则触发「昨日00:00 ~ 当日00:00」区间；当日幂等以 review_reports 落库记录
  （latest_review_period_end）为准，重启不重复；
- run_now：手动触发；无参维持昨日区间，有参（人工补跑历史区间）校验后按指定区间跑；
  与每日触发共用同一把锁，进行中返回忙（server 层映 409）；
- 单次触发异常吞掉记日志，护住巡检循环（复盘失败不影响交易决策循环）。
"""

from __future__ import annotations

import asyncio
import time

from src.audit.logger import get_logger
from src.config import Settings
from src.memory.repo import Repo
from src.review.agent import ReviewAgent

logger = get_logger(__name__)

_SECONDS_PER_DAY = 86400


def local_day_start(ts: float) -> float:
    """ts 所在自然日的本地 00:00 时间戳。"""
    lt = time.localtime(ts)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


def daily_fire_ts(daily_time: str, ts: float) -> float:
    """ts 当日的触发时刻（本地时间 HH:MM）时间戳。"""
    hour, minute = daily_time.split(":")
    lt = time.localtime(ts)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, int(hour), int(minute), 0, 0, 0, -1))


def _valid_period(start: float | None, end: float | None) -> bool:
    """人工补跑区间校验：两端齐全、为数字（拒绝 bool）且 start < end。"""
    if start is None or end is None:
        return False
    if isinstance(start, bool) or isinstance(end, bool):
        return False
    if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
        return False
    return start < end


class ReviewScheduler:
    """复盘调度器：触发逻辑集中在可注入时间的 _tick，巡检循环只是 sleep + 调用。"""

    def __init__(self, settings: Settings, agent: ReviewAgent, repo: Repo) -> None:
        self._settings = settings
        self._agent = agent
        self._repo = repo
        self._lock = asyncio.Lock()

    async def run_forever(self) -> None:
        """巡检主循环：每分钟检查是否到点；单次异常吞掉记日志，护住循环。"""
        while True:
            await asyncio.sleep(60)
            try:
                await self._tick()
            except Exception:
                logger.exception("复盘调度巡检异常")

    async def _tick(self, now: float | None = None) -> None:
        """单次巡检：到点且当日未复盘则触发昨日区间复盘（now 可注入，供测试）。"""
        if not self._settings.review.enabled:
            return
        now = time.time() if now is None else now
        day_start = local_day_start(now)
        if now < daily_fire_ts(self._settings.review.daily_time, now):
            return  # 未到当日触发时刻
        latest = await self._repo.review.latest_review_period_end()
        if latest is not None and latest >= day_start:
            return  # 当日已复盘（落库幂等，重启不重复）
        if self._lock.locked():
            return  # 手动触发进行中：跳过本次，下一分钟巡检再试
        async with self._lock:
            await self._agent.run(day_start - _SECONDS_PER_DAY, day_start)

    async def run_now(
        self, period_start: float | None = None, period_end: float | None = None
    ) -> dict:
        """手动触发复盘；进行中返回忙（error_code='busy'，server 层映 409）。

        无参维持昨日区间；有参（人工补跑历史区间）先校验（数字且 start < end），
        非法返回 error_code='invalid_period'（server 层映 422），不触发 agent。
        """
        if period_start is None and period_end is None:
            day_start = local_day_start(time.time())
            period_start, period_end = day_start - _SECONDS_PER_DAY, day_start
        elif not _valid_period(period_start, period_end):
            return {
                "started": False,
                "error": "复盘区间非法（需两端齐全、为数字且 start < end）",
                "error_code": "invalid_period",
            }
        if self._lock.locked():
            return {"started": False, "error": "复盘进行中", "error_code": "busy"}
        async with self._lock:
            result = await self._agent.run(period_start, period_end)
        # LLM 未配置时复盘未实际开始，started 诚实为 False（按结构化 error_code 判定）
        started = result.get("error_code") != "llm_not_configured"
        return {"started": started, **result}
