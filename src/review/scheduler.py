"""复盘调度：按间隔天数定时触发 + 手动触发，asyncio.Lock 防重入。

- run_forever：每 60s 巡检一次（镜像 paper.funding_patrol.funding_loop），到点且距上次复盘
  已满 interval_days 则触发「最近 interval_days 天（对齐当日 00:00）」区间；
  幂等以 review_reports 落库记录（latest_review_period_end）为准，重启不重复；
- start_now：手动触发（点火即返回，后台任务执行，HTTP 断连不影响生成）；无参维持最近
  interval_days 天区间，有参（人工补跑历史区间）校验后按指定区间跑；后台任务与定时触发
  共用同一把锁（点火到取锁的窗口由预留标志补位），进行中同步返回忙（server 层映 409），
  不排队等锁；
- 单次触发异常吞掉记日志，护住巡检循环（复盘失败不影响交易决策循环）。
"""

from __future__ import annotations

import asyncio
import time
import uuid

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
            None，就地初始化调度器依赖、asyncio 防重入锁、手动点火预留标志与后台任务引用
        """
        self._settings = settings
        self._agent = agent
        self._repo = repo
        self._lock = asyncio.Lock()
        # 手动点火预留：start_now 同步置位、后台任务 done 回调清位；
        # 覆盖点火到任务取锁之间的窗口，替代已废弃的调用方持锁跨任务转移
        self._manual_reserved = False
        self._manual_task: asyncio.Task[None] | None = None
        # 手动点火的预分配轮次编号与复盘区间：shutdown 补记「首次执行前被取消」终态用
        self._manual_round_id: str | None = None
        self._manual_period: tuple[float, float] | None = None

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
        if self._manual_reserved or self._lock.locked():
            return  # 手动触发进行中（含已点火未取锁的预留窗口）：跳过本次，下一分钟巡检再试
        async with self._lock:
            await self._agent.run(day_start - span, day_start)

    async def start_now(
        self, period_start: float | None = None, period_end: float | None = None
    ) -> dict:
        """手动触发复盘（点火即返回）：同步校验后点火后台任务，调用方被取消不影响生成。

        无参维持最近 interval_days 天区间；有参（人工补跑历史区间）先校验
        （数字且 start < end），非法同步返回 error_code='invalid_period'（server 层映 422），
        不点火。生成进度与结果经 WS 事件、/live 轮询与报告列表呈现，不在本调用中等待。
        执行权采用「预留标志 + 任务内自取锁」两段式：本方法只同步校验并置预留标志，
        自身不做任何 await 锁操作——锁不再由调用方任务持有后转移给后台任务，
        杜绝点火后、任务首次执行前被取消导致的锁永久占用。

        参数：
            period_start: float | None，复盘区间起点
            period_end: float | None，复盘区间终点
        返回：
            dict：点火成功 {"started": True, "period_start": ..., "period_end": ...,
            "round_id": 预分配的审计轮次编号（32 位 hex），与后台 WS review_round_start
            事件同一身份，前端据此认轮}；
            同步失败 {"started": False, "error": ..., "error_code": ...}，error_code 为
            llm_not_configured（未配置 LLM）、busy（进行中，server 层映 409）或
            invalid_period（区间非法，server 层映 422）
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
        if not self._agent.llm_configured:
            return {"started": False, "error": "LLM 未配置", "error_code": "llm_not_configured"}
        if self._manual_reserved or self._lock.locked():
            return {"started": False, "error": "复盘进行中", "error_code": "busy"}
        # busy 判定与置预留在同一同步段内完成（同一事件循环内原子，不让出执行权）：
        # 定时巡检看到预留即跳过，不会在预留与后台任务取锁之间插队，
        # 不排队语义与原先的持锁模式等价。
        self._manual_reserved = True
        round_id = uuid.uuid4().hex  # 预分配：点火响应与轮始事件携带同一身份
        self._manual_round_id = round_id  # 供 shutdown 补记「首次执行前被取消」终态
        self._manual_period = (period_start, period_end)
        task = asyncio.create_task(self._run_manual(period_start, period_end, round_id))
        # done 回调无条件清预留：任务正常结束、异常或首次执行前被取消，回调都会执行
        task.add_done_callback(self._release_manual_reservation)
        self._manual_task = task
        return {
            "started": True,
            "period_start": period_start,
            "period_end": period_end,
            "round_id": round_id,
        }

    def _release_manual_reservation(self, _task: asyncio.Task[None]) -> None:
        """手动后台任务完成回调：无条件清除点火预留标志。

        参数：
            _task: asyncio.Task[None]，已结束（含异常/取消）的手动后台任务，本回调不读取

        返回：
            None：就地清除预留标志；任务以任何方式结束事件循环都会触发本回调，
            预留标志永不泄漏
        """
        self._manual_reserved = False

    async def _run_manual(self, period_start: float, period_end: float, round_id: str) -> None:
        """后台执行手动复盘：任务内自取锁包住 agent.run；取消原样抛出，意外异常记日志就地取回。

        锁只在协程体内由本任务持有：任务在首次执行前被取消时协程体根本不进入，
        锁从未持有、无需释放（点火预留标志由 start_now 注册的 done 回调清理）。

        参数：
            period_start: float，复盘区间起点
            period_end: float，复盘区间终点
            round_id: str，点火时预分配的审计轮次编号，透传给 agent.run

        返回：
            None：意外异常记 logger.exception 就地取回，任务异常永远被取回，
            杜绝 never-retrieved 噪音

        异常：
            asyncio.CancelledError：执行中被取消（如停机 shutdown）记日志后原样抛出，
            保留取消语义（task.cancelled() 为真）；已持有的锁由 async with 退出释放；
            取消结果由 shutdown 的 gather(return_exceptions=True) 取回，不刷
            never-retrieved 噪音
        """
        try:
            async with self._lock:
                await self._agent.run(period_start, period_end, round_id=round_id)
        except asyncio.CancelledError:
            logger.info("手动复盘后台任务被取消（period_start=%s）", period_start)
            raise
        except Exception:
            logger.exception("手动复盘后台任务异常（period_start=%s）", period_start)

    async def shutdown(self) -> None:
        """取消进行中的手动后台任务并等待其收尾（停机序列调用，须在数据库关闭前）。

        任务从未首次执行时 begin_round 从未运行、agent 取消收尾也未进入，预分配
        round_id 查无任何记录——gather 之后由 _record_prestart_cancellation 补写
        取消终态报告，保证点火过的轮次必留有终态痕迹。

        参数：无

        返回：
            None：无进行中任务时立即返回；否则取消任务并 gather 取回结果，
            随后按需补记关机取消终态
        """
        task = self._manual_task
        if task is None or task.done():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await self._record_prestart_cancellation()

    async def _record_prestart_cancellation(self) -> None:
        """关机补记：手动任务在首次执行前被取消时，为预分配轮次补写取消终态失败报告。

        判重两道闸：审计轮已存在（begin_round 已跑，agent 取消收尾已负责终态）或
        该轮已有报告记录（begin_round 前失败已落失败报告，报告行带预分配 round_id）
        均跳过；两者都无才补写。补记自身异常只记日志不扩散（关机序列不得被打断）。

        参数：无

        返回：
            None：就地写入取消终态失败报告；无需补记或补记失败时无副作用/仅记日志
        """
        round_id = self._manual_round_id
        period = self._manual_period
        if not round_id or period is None:
            return
        try:
            if await self._repo.get_audit_round(round_id) is not None:
                return  # begin_round 已跑：agent 取消收尾已负责终态
            if await self._repo.review.find_report_by_round_id(round_id) is not None:
                return  # begin_round 前失败已落失败报告（报告行带预分配 round_id）
            await self._repo.review.save_review_report(
                period[0],
                period[1],
                "{}",
                "",
                "none",
                error="手动复盘在开始执行前被关机取消",
                round_id=round_id,
            )
        except Exception:
            logger.exception("关机补记手动复盘取消终态失败（round_id=%s）", round_id)
