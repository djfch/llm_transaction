"""复盘调度：按间隔天数定时触发 + 手动触发，asyncio.Lock 防重入。

- run_forever：每 60s 巡检一次（镜像 paper.funding_patrol.funding_loop），到点且距上次复盘
  已满 interval_days 则触发「最近 interval_days 天（对齐当日 00:00）」区间；
  幂等以 review_reports 落库记录（latest_review_period_end）为准，重启不重复；
- run_now：手动触发；无参维持最近 interval_days 天区间，有参（人工补跑历史区间）
  校验后按指定区间跑；与定时触发共用同一把锁，进行中返回忙（server 层映 409）；
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
    """ts 所在自然日的本地 00:00 时间戳。

    参数：
        ts: float，时间戳
    返回：
        float，ts 所在自然日的本地 00:00 时间戳
    """
    lt = time.localtime(ts)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


def daily_fire_ts(daily_time: str, ts: float) -> float:
    """ts 当日的触发时刻（本地时间 HH:MM）时间戳。

    参数：
        daily_time: str，每日触发时刻
        ts: float，时间戳
    返回：
        float，ts 当日的触发时刻（本地时间 HH:MM）时间戳
    """
    hour, minute = daily_time.split(":")
    lt = time.localtime(ts)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, int(hour), int(minute), 0, 0, 0, -1))


def _valid_period(start: float | None, end: float | None) -> bool:
    """人工补跑区间校验：两端齐全、为数字（拒绝 bool）且 start < end。

    参数：
        start: float | None，人工补跑区间起点
        end: float | None，人工补跑区间终点
    返回：
        bool，人工补跑区间校验：两端齐全、为数字（拒绝 bool）且 start < end
    """
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
        """创建复盘调度器，注入配置、复盘 agent 与持久化仓库，并初始化防重入锁。

        参数：
            settings: Settings，全局配置（读取其中 review 段的开关、触发时刻与间隔天数）
            agent: ReviewAgent，复盘 agent（单次触发时调用其 run 执行复盘）
            repo: Repo，持久化仓库（经 review 子仓库读取上次复盘区间，用于落库幂等判定）

        返回：
            None，就地初始化调度器依赖与 asyncio 防重入锁
        """
        self._settings = settings
        self._agent = agent
        self._repo = repo
        self._lock = asyncio.Lock()

    async def run_forever(self) -> None:
        """巡检主循环：每分钟检查是否到点；单次异常吞掉记日志，护住循环。

        参数：无
        返回：
            None，巡检主循环：每分钟检查是否到点；单次异常吞掉记日志，护住循环
        """
        while True:
            await asyncio.sleep(60)
            try:
                await self._tick()
            except Exception:
                logger.exception("复盘调度巡检异常")

    async def _tick(self, now: float | None = None) -> None:
        """单次巡检：到点且距上次复盘已满间隔天数则触发（now 可注入，供测试）。

        参数：
            now: float | None，可注入的当前时间戳
        返回：
            None，单次巡检：到点且距上次复盘已满间隔天数则触发（now 可注入，供测试）
        """
        if not self._settings.review.enabled:
            return
        now = time.time() if now is None else now
        day_start = local_day_start(now)
        if now < daily_fire_ts(self._settings.review.daily_time, now):
            return  # 未到当日触发时刻
        span = self._settings.review.interval_days * _SECONDS_PER_DAY
        latest = await self._repo.review.latest_review_period_end()
        # latest 先对齐到其所在自然日 00:00：人工补跑的 period_end 可为任意时刻，
        # 直接做秒差会把定时复盘多推迟一天；日对齐后按自然日计数（固定 86400 秒/天，
        # 隐含无夏令时假设，中国时区成立）
        if latest is not None and day_start - local_day_start(latest) < span:
            return  # 距上次复盘未满间隔天数（落库幂等，重启不重复）
        if self._lock.locked():
            return  # 手动触发进行中：跳过本次，下一分钟巡检再试
        async with self._lock:
            await self._agent.run(day_start - span, day_start)

    async def run_now(
        self, period_start: float | None = None, period_end: float | None = None
    ) -> dict:
        """手动触发复盘；进行中返回忙（error_code='busy'，server 层映 409）。

        无参维持最近 interval_days 天区间；有参（人工补跑历史区间）先校验
        （数字且 start < end），非法返回 error_code='invalid_period'（server 层映 422），
        不触发 agent。

        参数：
            period_start: float | None，复盘区间起点
            period_end: float | None，复盘区间终点
        返回：
            dict，手动触发复盘；进行中返回忙（error_code='busy'，server 层映 409）
        """
        if period_start is None and period_end is None:
            day_start = local_day_start(time.time())
            span = self._settings.review.interval_days * _SECONDS_PER_DAY
            period_start, period_end = day_start - span, day_start
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
