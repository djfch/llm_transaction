"""研报自动调度：市场开盘预设、自定义 UTC+8 时间、官方交易日与手动触发。"""

from __future__ import annotations

import asyncio
import time
from datetime import date, datetime, time as clock, timedelta
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from src.audit.logger import get_logger
from src.config import ROOT, FixedTimeSchedule, MarketOpenSchedule, ResearchSchedule, Settings
from src.memory.repo import Repo
from src.research.agent import ResearchAgent
from src.research.calendars import MarketCalendarProvider

logger = get_logger(__name__)

BEIJING_TZ = ZoneInfo("Asia/Shanghai")
_MARKET_OPEN = {
    "XTKS": (ZoneInfo("Asia/Tokyo"), clock(9, 0)),
    "XLON": (ZoneInfo("Europe/London"), clock(8, 0)),
    "XNYS": (ZoneInfo("America/New_York"), clock(9, 30)),
}


class CalendarLike(Protocol):
    """调度器依赖的官方交易日日历最小接口。"""

    async def refresh(self) -> None:
        """刷新官方日历缓存。

        参数：无

        返回：
            None：更新实现内部缓存
        """

    def is_trading_day(self, market: str, target: date) -> bool:
        """判断市场日期是否交易。

        参数：
            market: str，市场代码
            target: date，市场日期

        返回：
            bool：是否交易
        """
        ...

    def status(self) -> dict[str, Any]:
        """返回日历刷新状态。

        参数：无

        返回：
            dict[str, Any]：前端可展示状态
        """
        ...


def _fire_at(schedule: ResearchSchedule, target: date) -> datetime:
    """计算调度项在目标 UTC+8 日期对应的绝对触发时刻。

    参数：
        schedule: ResearchSchedule，市场预设或自定义项
        target: date，UTC+8 会话日期

    返回：
        datetime：转换到 UTC+8 的带时区触发时刻
    """
    if isinstance(schedule, FixedTimeSchedule):
        hour, minute = (int(part) for part in schedule.time.split(":"))
        return datetime.combine(target, clock(hour, minute), BEIJING_TZ)
    market_tz, open_time = _MARKET_OPEN[schedule.market]
    opening = datetime.combine(target, open_time, market_tz)
    return (opening - timedelta(minutes=schedule.lead_minutes)).astimezone(BEIJING_TZ)


def _calendar_code(schedule: ResearchSchedule) -> str | None:
    """取得调度项的交易日日历代码；daily 返回 None。

    参数：
        schedule: ResearchSchedule，市场预设或自定义项

    返回：
        str | None：XTKS/XLON/XNYS，或每天执行的 None
    """
    if isinstance(schedule, MarketOpenSchedule):
        return schedule.market
    return None if schedule.calendar == "daily" else schedule.calendar


class ResearchScheduler:
    """严格命中目标分钟的研报调度器；错过、忙碌或休市均不补跑。"""

    def __init__(
        self,
        settings: Settings,
        agent: ResearchAgent,
        repo: Repo,
        *,
        calendar: CalendarLike | None = None,
        cache_path: Path | None = None,
    ) -> None:
        """保存共享配置、Agent、仓储与官方日历，并创建防重入锁。

        参数：
            settings: Settings，共享运行配置，保存后原地热更新
            agent: ResearchAgent，研报执行入口
            repo: Repo，研报幂等查询仓储
            calendar: CalendarLike | None，可注入的交易日日历
            cache_path: Path | None，默认日历缓存路径

        返回：
            None：就地初始化调度器
        """
        self._settings = settings
        self._agent = agent
        self._repo = repo
        path = cache_path or ROOT / "data" / "market_calendar_cache.json"
        self._calendar = calendar or MarketCalendarProvider(path)
        self._lock = asyncio.Lock()
        self._last_refresh_date: date | None = None

    async def run_forever(self) -> None:
        """立即检查当前分钟，刷新日历，此后对齐自然分钟巡检并每日刷新。

        参数：无

        返回：
            None：长期运行直至任务被取消
        """
        await self._safe_refresh()
        await self._safe_tick()
        while True:
            delay = 60 - (time.time() % 60) + 0.01
            await asyncio.sleep(delay)
            await self._safe_tick()
            local = datetime.now(BEIJING_TZ)
            if local.time() >= clock(0, 10) and self._last_refresh_date != local.date():
                await self._safe_refresh()

    async def _safe_tick(self) -> None:
        """执行一次巡检并吞掉异常，保护长期任务。

        参数：无

        返回：
            None：异常仅记录日志
        """
        try:
            await self.tick()
        except Exception:
            logger.exception("研报调度巡检异常")

    async def _safe_refresh(self) -> None:
        """刷新官方日历并吞掉顶层异常，保留缓存降级能力。

        参数：无

        返回：
            None：刷新后记录 UTC+8 日期
        """
        try:
            await self._calendar.refresh()
        except Exception:
            logger.exception("官方交易日日历刷新异常")
        self._last_refresh_date = datetime.now(BEIJING_TZ).date()

    async def tick(self, now: float | None = None) -> None:
        """检查当前绝对分钟，命中一个启用调度且当日未执行时生成研报。

        参数：
            now: float | None，可注入 Unix 秒；省略时使用当前时间

        返回：
            None：最多触发一次研报；错过不补跑
        """
        cfg = self._settings.research
        if not cfg.enabled:
            return
        stamp = time.time() if now is None else now
        local = datetime.fromtimestamp(stamp, BEIJING_TZ)
        due = [item for item in cfg.schedules if self._is_due(item, local)]
        if not due:
            return
        if len(due) > 1:
            logger.warning(
                "多个研报调度同时到期，仅执行首项：%s", ",".join(item.id for item in due)
            )
        schedule = due[0]
        if self._lock.locked():
            logger.info("研报调度命中但 Agent 正忙，按不补跑规则跳过：%s", schedule.id)
            return
        # asyncio.Lock 在未占用时会同步取得锁；必须在首次 await 前占有执行权，
        # 否则手动任务可在数据库查询期间抢锁，自动任务随后排队形成补跑。
        await self._lock.acquire()
        try:
            current = next((item for item in cfg.schedules if item.id == schedule.id), None)
            if not cfg.enabled or current is None or not self._is_due(current, local):
                return
            claimed = await self._repo.research.claim_schedule_run(current.id, local.date())
            if not claimed:
                return
            current = next((item for item in cfg.schedules if item.id == schedule.id), None)
            if not cfg.enabled or current is None or not self._is_due(current, local):
                return
            await self._agent.run(report_type=current.id, hours=24)
        finally:
            self._lock.release()

    def _is_due(self, schedule: ResearchSchedule, now: datetime) -> bool:
        """判断启用项是否在当前分钟到期且符合日期规则。

        参数：
            schedule: ResearchSchedule，待判断调度项
            now: datetime，当前 UTC+8 时间

        返回：
            bool：是否应在本分钟触发
        """
        if not schedule.enabled:
            return False
        calendar = _calendar_code(schedule)
        if calendar is not None and not self._calendar.is_trading_day(calendar, now.date()):
            return False
        return int(_fire_at(schedule, now.date()).timestamp() // 60) == int(now.timestamp() // 60)

    def status(self, now: float | None = None) -> dict[str, Any]:
        """返回配置中心所需总开关、各项下一次执行时间与日历状态。

        参数：
            now: float | None，可注入当前 Unix 秒

        返回：
            dict[str, Any]：调度状态接口响应
        """
        stamp = time.time() if now is None else now
        current = datetime.fromtimestamp(stamp, BEIJING_TZ)
        items = [
            {
                "id": item.id,
                "kind": item.kind,
                "enabled": item.enabled,
                "next_run_at": self._next_run(item, current),
            }
            for item in self._settings.research.schedules
        ]
        return {
            "enabled": self._settings.research.enabled,
            "items": items,
            "calendar": self._calendar.status(),
        }

    def _next_run(self, schedule: ResearchSchedule, now: datetime) -> float | None:
        """搜索未来 370 天内下一个符合日期规则的触发时刻。

        参数：
            schedule: ResearchSchedule，待搜索调度项
            now: datetime，当前 UTC+8 时间

        返回：
            float | None：下一次 Unix 秒；禁用或范围内无日期时为 None
        """
        if not schedule.enabled:
            return None
        calendar = _calendar_code(schedule)
        for offset in range(371):
            target = now.date() + timedelta(days=offset)
            fire = _fire_at(schedule, target)
            if fire <= now:
                continue
            if calendar is None or self._calendar.is_trading_day(calendar, target):
                return fire.timestamp()
        return None

    async def run_now(self, report_type: str = "manual", hours: int = 24) -> dict:
        """手动触发研报；不受自动开关和交易日限制，进行中返回 busy。

        参数：
            report_type: str，研报类型
            hours: int，回看小时数

        返回：
            dict：started 与 Agent 结构化结果
        """
        if self._lock.locked():
            return {"started": False, "error": "研报生成中", "error_code": "busy"}
        async with self._lock:
            result = await self._agent.run(report_type=report_type, hours=hours)
        started = result.get("error_code") != "llm_not_configured"
        return {"started": started, **result}
