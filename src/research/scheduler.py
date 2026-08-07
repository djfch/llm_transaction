"""研报调度：每日三盘口（亚盘/欧盘/美盘，北京时间）定时触发 + 手动触发，asyncio.Lock 防重入。

- run_forever：每 60s 巡检一次（镜像 review.scheduler 模式），单次异常吞掉记日志护住循环
  （研报失败不影响交易决策循环）；
- _tick：enabled 热开关每 tick 现读 settings；取当日「已到触发时刻的最新一个盘口」，
  以 research_reports 落库记录（has_report_since，当日 00:00 锚点）幂等跳过；
  只看最新到点盘口、不回看更早盘口——重启补跑只补最近一篇，不连补三篇；
- 美盘顺延：us_dst_adjust=True 且触发时刻美国为冬令时（非 DST）时，美盘触发时刻 +1h；
  北京时间与纽约时间的换算在纯函数内完成（_slot_fire_ts 可注入时间戳测试）；
- run_now：手动触发，与定时共用同一把锁，进行中返回忙（error_code='busy'，server 层映 409）。
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from src.audit.logger import get_logger
from src.config import ResearchConfig, Settings
from src.memory.repo import Repo
from src.research.agent import ResearchAgent

logger = get_logger(__name__)

_NY_TZ = ZoneInfo("America/New_York")


def _day_start(ts: float) -> float:
    """ts 所在自然日的本地 00:00 时间戳（镜像 review.scheduler.local_day_start）。"""
    lt = time.localtime(ts)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


def _is_ny_dst(ts: float) -> bool:
    """ts 时刻纽约是否处于夏令时（dst() 非零即夏令时；绝对时刻换算，与本地时区无关）。"""
    return bool(datetime.fromtimestamp(ts, tz=_NY_TZ).dst())


def _slot_fire_ts(daily_time: str, ts: float, *, us_dst_adjust: bool = False) -> float:
    """ts 当日盘口触发时刻（本地 HH:MM）时间戳。

    us_dst_adjust=True 且触发时刻纽约为冬令时（非 DST）时 +1h（美盘冬令时顺延）；
    隐含触发时刻 +1h 不跨自然日假设（默认 21:00→22:00 成立）。
    """
    hour, minute = daily_time.split(":")
    lt = time.localtime(ts)
    fire = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, int(hour), int(minute), 0, 0, 0, -1))
    if us_dst_adjust and not _is_ny_dst(fire):
        fire += 3600
    return fire


class ResearchScheduler:
    """研报调度器：触发逻辑集中在可注入时间的 _tick，巡检循环只是 sleep + 调用。"""

    def __init__(self, settings: Settings, agent: ResearchAgent, repo: Repo) -> None:
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
                logger.exception("研报调度巡检异常")

    async def _tick(self, now: float | None = None) -> None:
        """单次巡检：最新到点盘口当日未跑则触发（now 可注入，供测试）。"""
        cfg = self._settings.research  # enabled 等热开关每 tick 现读
        if not cfg.enabled:
            return
        now = time.time() if now is None else now
        slot = await self._due_slot(cfg, now)
        if slot is None:
            return
        if self._lock.locked():
            return  # 手动触发进行中：跳过本次，下一分钟巡检再试
        async with self._lock:
            await self._agent.run(report_type=slot, hours=24)

    async def _due_slot(self, cfg: ResearchConfig, now: float) -> str | None:
        """当日最新到点盘口未跑则返回其 report_type；未跑判定以落库成功研报（当日锚点）为准。"""
        slot = self._latest_due_slot(cfg, now)
        if slot is None:
            return None
        if await self._repo.research.has_report_since(slot, _day_start(now)):
            return None
        return slot

    def _latest_due_slot(self, cfg: ResearchConfig, now: float) -> str | None:
        """当日已到触发时刻的最新盘口名（全部未到点返回 None；不回看更早盘口）。"""
        fires = [
            ("asia_open", _slot_fire_ts(cfg.time_asia, now)),
            ("europe_open", _slot_fire_ts(cfg.time_europe, now)),
            ("us_open", _slot_fire_ts(cfg.time_us, now, us_dst_adjust=cfg.us_dst_adjust)),
        ]
        fires.sort(key=lambda p: p[1])  # 按触发时刻排序，防配置时刻乱序
        due = [name for name, fire in fires if now >= fire]
        return due[-1] if due else None

    async def run_now(self, report_type: str = "manual", hours: int = 24) -> dict:
        """手动触发研报；进行中返回忙（error_code='busy'，server 层映 409）。

        agent.run 的结构化结果（ok/report_id/error_code 等）原样并入返回。
        """
        if self._lock.locked():
            return {"started": False, "error": "研报生成中", "error_code": "busy"}
        async with self._lock:
            result = await self._agent.run(report_type=report_type, hours=hours)
        # LLM 未配置时研报未实际开始，started 诚实为 False（按结构化 error_code 判定）
        started = result.get("error_code") != "llm_not_configured"
        return {"started": started, **result}
