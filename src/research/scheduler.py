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
from src.research.calendars import CalendarRefreshResult, MarketCalendarProvider

logger = get_logger(__name__)

BEIJING_TZ = ZoneInfo("Asia/Shanghai")
_MARKET_OPEN = {
    "XTKS": (ZoneInfo("Asia/Tokyo"), clock(9, 0)),
    "XLON": (ZoneInfo("Europe/London"), clock(8, 0)),
    "XNYS": (ZoneInfo("America/New_York"), clock(9, 30)),
}
_REFRESH_BACKOFF_MINUTES = (5, 15, 30, 60)


class CalendarLike(Protocol):
    """调度器依赖的官方交易日日历最小接口。"""

    async def refresh(self) -> CalendarRefreshResult:
        """刷新官方日历缓存。

        参数：无

        返回：
            CalendarRefreshResult：逐来源与缓存写入结果
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


def _market_calendar_date(calendar: str, fire: datetime) -> date:
    """把 UTC+8 触发时刻换算到目标市场当地日期，用于交易日判断。

    参数：
        calendar: str，市场日历代码
        fire: datetime，UTC+8 触发时刻

    返回：
        date：触发时刻在市场时区下的当地日期
    """
    return fire.astimezone(_MARKET_OPEN[calendar][0]).date()


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
        """保存共享配置、Agent、仓储与官方日历，并创建防重入锁与手动后台任务引用。

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
        self._manual_task: asyncio.Task[None] | None = None
        self._last_refresh_at: datetime | None = None
        self._next_refresh_at: datetime | None = None
        self._refresh_failures = 0

    async def run_forever(self) -> None:
        """立即检查当前分钟，刷新日历，此后对齐自然分钟巡检并每日刷新。

        参数：无

        返回：
            None：长期运行直至任务被取消
        """
        await self._safe_refresh(datetime.now(BEIJING_TZ))
        await self._safe_tick()
        while True:
            delay = 60 - (time.time() % 60) + 0.01
            await asyncio.sleep(delay)
            local = datetime.now(BEIJING_TZ)
            if self._should_refresh(local):
                await self._safe_refresh(local)
            await self._safe_tick()

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

    async def _safe_refresh(self, current: datetime) -> bool:
        """刷新官方日历，失败时安排当日退避重试。

        参数：
            current: datetime，本次刷新对应的 UTC+8 时刻

        返回：
            bool：三家来源与缓存全部成功时为 True
        """
        try:
            result = await self._calendar.refresh()
        except Exception:
            logger.exception("官方交易日日历刷新异常")
        else:
            if result.complete:
                self._last_refresh_at = current
                self._next_refresh_at = None
                self._refresh_failures = 0
                return True
        index = min(self._refresh_failures, len(_REFRESH_BACKOFF_MINUTES) - 1)
        self._refresh_failures += 1
        daily_at = datetime.combine(current.date(), clock(0, 10), BEIJING_TZ)
        retry_at = current + timedelta(minutes=_REFRESH_BACKOFF_MINUTES[index])
        self._next_refresh_at = daily_at if current < daily_at else retry_at
        return False

    def _should_refresh(self, current: datetime) -> bool:
        """判断每日 00:10 刷新或失败退避重试是否到期。

        参数：
            current: datetime，当前 UTC+8 时刻

        返回：
            bool：需要在本分钟先刷新日历时为 True
        """
        daily_at = datetime.combine(current.date(), clock(0, 10), BEIJING_TZ)
        if current < daily_at:
            return False
        last = self._last_refresh_at
        if last is not None and last.date() == current.date() and last >= daily_at:
            return False
        return self._next_refresh_at is None or current >= self._next_refresh_at

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
        fire = _fire_at(schedule, now.date())
        if calendar is not None and not self._calendar.is_trading_day(
            calendar, _market_calendar_date(calendar, fire)
        ):
            return False
        return int(fire.timestamp() // 60) == int(now.timestamp() // 60)

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
            if calendar is None or self._calendar.is_trading_day(
                calendar, _market_calendar_date(calendar, fire)
            ):
                return fire.timestamp()
        return None

    async def start_now(self, report_type: str = "manual", hours: int = 24) -> dict:
        """手动触发研报（点火即返回）：同步校验后点火后台任务，调用方被取消不影响生成。

        不受自动开关和交易日限制；生成进度与结果经 WS 事件、/live 轮询与报告列表呈现，
        不在本调用中等待。

        参数：
            report_type: str，研报类型
            hours: int，回看小时数

        返回：
            dict：点火成功 {"started": True, "report_type": ..., "hours": ...}；
            同步失败 {"started": False, "error": ..., "error_code": ...}，
            error_code 为 llm_not_configured（未配置 LLM）或 busy（已有生成进行中）
        """
        if not self._agent.llm_configured:
            return {"started": False, "error": "LLM 未配置", "error_code": "llm_not_configured"}
        if self._lock.locked():
            return {"started": False, "error": "研报生成中", "error_code": "busy"}
        # asyncio.Lock 在未占用时会同步取得锁；必须在首次让出前占有执行权（同 tick 模式），
        # 否则自动调度可在让出窗口抢锁，本任务随后排队等锁，破坏不排队语义。
        await self._lock.acquire()
        self._manual_task = asyncio.create_task(self._run_manual(report_type, hours))
        return {"started": True, "report_type": report_type, "hours": hours}

    async def _run_manual(self, report_type: str, hours: int) -> None:
        """后台执行手动研报：finally 释放锁，取消与意外异常只记日志、就地取回。

        参数：
            report_type: str，研报类型
            hours: int，回看小时数

        返回：
            None：CancelledError 记日志后吞掉（agent.run 已做取消收尾并落失败报告），
            意外异常记 logger.exception；任务异常永远被取回，杜绝 never-retrieved 噪音
        """
        try:
            await self._agent.run(report_type=report_type, hours=hours)
        except asyncio.CancelledError:
            logger.info("手动研报后台任务被取消（report_type=%s）", report_type)
        except Exception:
            logger.exception("手动研报后台任务异常（report_type=%s）", report_type)
        finally:
            self._lock.release()

    async def shutdown(self) -> None:
        """取消进行中的手动后台任务并等待其收尾（停机序列调用，须在数据库关闭前）。

        参数：无

        返回：
            None：无进行中任务时立即返回；否则取消任务并 gather 取回结果
        """
        task = self._manual_task
        if task is None or task.done():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
