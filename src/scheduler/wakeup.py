"""asyncio 唤醒调度器：定时唤醒 + 外部抢醒，单轮防重入。

两种唤醒源：
- 定时唤醒：每轮决策结束时按 LLM 自设分钟数（set_next_wake，经 SchedulerConfig
  min/max 钳制）重新武装定时器；本轮 LLM 未设时用 default_wake_minutes。
- 抢醒：价格触发器等外部事件调用 wake_now(reason)，立即唤醒（不等待定时器）。

防重入：begin_round() 到 end_round() 之间，定时器到期的唤醒事件一律丢弃；
wake_now 抢醒不立即生效，记为 pending（多个只留最后一个），轮末补一次唤醒
——价格触发器触发即删，若轮内直接丢弃会让预警静默蒸发。

注意：wake_now / set_next_wake 非线程安全，须在事件循环线程内调用。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from src.config import SchedulerConfig

logger = logging.getLogger(__name__)

WakeCallback = Callable[[str], Awaitable[None]]
SleepFn = Callable[[float], Awaitable[None]]


class WakeupScheduler:
    """唤醒调度器：start/stop 生命周期管理，stop 时取消定时任务。"""

    def __init__(
        self,
        config: SchedulerConfig,
        on_wake: WakeCallback,
        sleep_fn: SleepFn = asyncio.sleep,
    ) -> None:
        """初始化唤醒调度器状态、回调与可注入时间源。

        参数：
            config: SchedulerConfig，调度器配置
            on_wake: WakeCallback，唤醒回调
            sleep_fn: SleepFn，可注入的异步等待函数

        返回：
            None，初始化唤醒调度器状态、回调与可注入时间源
        """
        self._config = config
        self._on_wake = on_wake
        self._sleep = sleep_fn
        self._wake_event = asyncio.Event()
        self._wake_reason = ""
        self._pending_minutes: int | None = None  # 本轮 LLM 设置的下次唤醒分钟数
        self._pending_wake: str | None = None  # 决策轮内到达的抢醒原因（轮末补一次唤醒）
        self._in_round = False
        self._running = False
        self._loop_task: asyncio.Task[None] | None = None
        self._timer_task: asyncio.Task[None] | None = None

    @property
    def is_running(self) -> bool:
        """报告调度器当前是否处于运行状态。

        参数：无

        返回：
            bool：True 表示已启动（start 之后、stop 之前）；False 表示未启动或已停止
        """
        return self._running

    @property
    def in_round(self) -> bool:
        """报告当前是否正处于一轮决策执行期间（防重入窗口）。

        参数：无

        返回：
            bool：True 表示处于 begin_round 到 end_round 之间，期间定时器到期被丢弃、
            外部抢醒记为 pending
        """
        return self._in_round

    # ---------- 生命周期 ----------

    async def start(self) -> None:
        """启动调度器：武装默认定时器并启动调度循环。重复调用为空操作。

        参数：无

        返回：
            None，启动调度器：武装默认定时器并启动调度循环。重复调用为空操作
        """
        if self._running:
            return
        self._running = True
        self._wake_event.clear()
        self._pending_wake = None
        self._loop_task = asyncio.create_task(self._run())
        self._arm_timer(self._clamp(self._config.default_wake_minutes))

    async def stop(self) -> None:
        """停止调度器：取消定时任务并等待调度循环退出（进行中的决策轮会先跑完）。

        参数：无

        返回：
            None，停止调度器：取消定时任务并等待调度循环退出（进行中的决策轮会先跑完）
        """
        self._running = False
        if self._timer_task is not None:
            self._timer_task.cancel()
            await asyncio.gather(self._timer_task, return_exceptions=True)
            self._timer_task = None
        if self._loop_task is not None:
            self._wake_event.set()  # 唤醒循环，使其检查退出条件
            await self._loop_task
            self._loop_task = None

    # ---------- 唤醒源 ----------

    def wake_now(self, reason: str) -> bool:
        """外部抢醒（如价格触发器）。返回是否被接受。

        决策轮进行中到达的请求不立即唤醒（防重入），记为 pending 并在轮末补一次唤醒；
        轮内多个 pending 只保留最后一个原因（合并为一轮补充决策）。

        参数：
            reason: str，指标短名单修订原因

        返回：
            bool，外部抢醒（如价格触发器）。返回是否被接受。  决策轮进行中到达的请求不立即唤醒（防重入），记为 pending 并在轮末补一次唤醒； 轮内多个 pending 只保留最后一个原因（合并为一轮补充决策）

        """
        return self._fire(reason, allow_pending=True)

    def set_next_wake(self, minutes: int) -> int:
        """LLM 设置下次定时唤醒分钟数，返回钳制后实际生效的值。

        仅记录，本轮结束（end_round）重新武装定时器时才生效；本轮未调用则用默认值。

        参数：
            minutes: int，唤醒间隔分钟数

        返回：
            int，LLM 设置下次定时唤醒分钟数，返回钳制后实际生效的值。  仅记录，本轮结束（end_round）重新武装定时器时才生效；本轮未调用则用默认值

        """
        clamped = self._clamp(minutes)
        if clamped != minutes:
            logger.info("唤醒间隔 %d 分钟越界，钳制为 %d 分钟", minutes, clamped)
        self._pending_minutes = clamped
        return clamped

    # ---------- 防重入 ----------

    def begin_round(self) -> None:
        """开始一轮决策：期间定时器到期丢弃，wake_now 抢醒记 pending（轮末补唤醒）。

        参数：无

        返回：
            None，开始一轮决策：期间定时器到期丢弃，wake_now 抢醒记 pending（轮末补唤醒）
        """
        self._in_round = True

    def end_round(self) -> None:
        """结束一轮决策：解除防重入，重新武装定时器；轮内有 pending 抢醒则补一次唤醒。

        参数：无

        返回：
            None，结束一轮决策：解除防重入，重新武装定时器；轮内有 pending 抢醒则补一次唤醒
        """
        if not self._in_round:
            return
        self._in_round = False
        minutes = self._pending_minutes
        self._pending_minutes = None
        if minutes is None:
            minutes = self._config.default_wake_minutes
        self._arm_timer(self._clamp(minutes))
        pending, self._pending_wake = self._pending_wake, None
        if pending is not None:
            self._fire(pending)  # 补唤醒：已出决策轮；若期间 stop 则自然丢弃

    # ---------- 内部实现 ----------

    def _clamp(self, minutes: int) -> int:
        """把唤醒间隔钳制到 [min_wake_minutes, max_wake_minutes]。

        参数：
            minutes: int，唤醒间隔分钟数

        返回：
            int，把唤醒间隔钳制到 [min_wake_minutes, max_wake_minutes]
        """
        return max(self._config.min_wake_minutes, min(self._config.max_wake_minutes, minutes))

    def _fire(self, reason: str, allow_pending: bool = False) -> bool:
        """唤醒统一入口：仅在运行中且不在决策轮内才立即生效。

        决策轮内：allow_pending（外部抢醒）记 pending 待轮末补唤醒；否则（定时器到期）丢弃。

        参数：
            reason: str，指标短名单修订原因
            allow_pending: bool，处于决策轮时是否允许记录待补唤醒

        返回：
            bool，唤醒统一入口：仅在运行中且不在决策轮内才立即生效。  决策轮内：allow_pending（外部抢醒）记 pending 待轮末补唤醒；否则（定时器到期）丢弃

        """
        if self._running and not self._in_round:
            self._wake_reason = reason
            self._wake_event.set()
            return True
        if allow_pending and self._running:
            self._pending_wake = reason
            logger.info("决策轮内收到抢醒，记为 pending（轮末补唤醒）：%s", reason)
            return True
        logger.info(
            "丢弃唤醒事件（运行中=%s, 决策轮内=%s）：%s", self._running, self._in_round, reason
        )
        return False

    def _arm_timer(self, minutes: int) -> None:
        """以指定分钟数重新武装定时器（先取消旧的，避免抢醒后旧定时器重复触发）。

        参数：
            minutes: int，唤醒间隔分钟数

        返回：
            None，以指定分钟数重新武装定时器（先取消旧的，避免抢醒后旧定时器重复触发）
        """
        if self._timer_task is not None:
            self._timer_task.cancel()
            self._timer_task = None
        if self._running:
            self._timer_task = asyncio.create_task(self._timer_loop(minutes))

    async def _timer_loop(self, minutes: int) -> None:
        """定时器协程：睡眠指定分钟数后触发一次定时唤醒，被重新武装或停止时静默退出。

        参数：
            minutes: int，本次定时唤醒的间隔分钟数（由 _arm_timer 传入，已钳制）

        返回：None，到期时经 _fire 置起唤醒事件；被取消（重新武装/停止）时直接返回
        """
        try:
            await self._sleep(minutes * 60)
        except asyncio.CancelledError:
            return
        self._fire(f"timer:{minutes}min")

    async def _run(self) -> None:
        """调度循环：等待唤醒事件并派发决策轮。

        参数：无

        返回：
            None，调度循环：等待唤醒事件并派发决策轮
        """
        while self._running:
            await self._wake_event.wait()
            self._wake_event.clear()
            if not self._running:
                break
            reason = self._wake_reason
            self._wake_reason = ""
            await self._run_round(reason)

    async def _run_round(self, source: str) -> None:
        """执行一轮决策：回调期间自动进入决策轮（防重入），异常不拖垮调度循环。

        参数：
            source: str，成交来源

        返回：
            None，执行一轮决策：回调期间自动进入决策轮（防重入），异常不拖垮调度循环
        """
        self.begin_round()
        try:
            await self._on_wake(source)
        except Exception:
            logger.exception("唤醒回调异常（source=%s）", source)
        finally:
            self.end_round()
