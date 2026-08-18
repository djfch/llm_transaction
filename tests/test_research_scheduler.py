"""研报严格分钟调度、市场时区、交易日与手动触发测试。"""

import asyncio

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src.config import AuditConfig, FixedTimeSchedule, ResearchConfig, Settings
from src.audit.trail import AuditTrail
from src.memory import Database, Repo
from src.research.agent import ResearchAgent
from src.research.calendars import CalendarRefreshResult
from src.research.prompts import ResearchPromptLoader
from src.research.providers.base import ResearchDataProvider
from src.research.scheduler import ResearchScheduler

BEIJING = ZoneInfo("Asia/Shanghai")


class StubAgent:
    """记录调用并返回固定成功结果的研报 Agent 边界桩。"""

    def __init__(self) -> None:
        """初始化空调用记录与已配置 LLM 标记。

        参数：无

        返回：
            None：就地创建调用列表
        """
        self.calls: list[dict] = []
        self.round_ids: list[str | None] = []  # 每次 run 收到的预分配 round_id（自动调度为 None）
        self.llm_configured = True

    async def run(
        self, report_type: str = "manual", hours: int = 24, *, round_id: str | None = None
    ) -> dict:
        """记录报告类型、回看小时与预分配轮次编号并返回成功结果。

        参数：
            report_type: str，研报类型
            hours: int，回看小时数
            round_id: str | None，调度器手动点火时预分配的审计轮次编号

        返回：
            dict：固定成功结果
        """
        self.calls.append({"report_type": report_type, "hours": hours})
        self.round_ids.append(round_id)
        return {"ok": True, "report_id": 1, "round_id": "r1", "direction": "中性"}


class StubCalendar:
    """按关闭日期集合判断交易日的日历边界桩。"""

    def __init__(self, closed: set[tuple[str, date]] | None = None) -> None:
        """初始化关闭日期集合。

        参数：
            closed: set[tuple[str, date]] | None，市场与休市日元组

        返回：
            None：就地保存休市集合
        """
        self.closed = closed or set()
        self.refresh_calls = 0

    async def refresh(self) -> CalendarRefreshResult:
        """模拟官方日历刷新成功。

        参数：无

        返回：
            CalendarRefreshResult：三家来源与缓存均成功
        """
        self.refresh_calls += 1
        return CalendarRefreshResult(("XTKS", "XLON", "XNYS"), {}, True)

    def is_trading_day(self, market: str, target: date) -> bool:
        """判断市场日期是否不在关闭集合且不是周末。

        参数：
            market: str，市场代码
            target: date，市场日期

        返回：
            bool：目标日期是否交易
        """
        return target.weekday() < 5 and (market, target) not in self.closed

    def status(self) -> dict:
        """返回固定健康日历状态。

        参数：无

        返回：
            dict：前端状态契约
        """
        return {"state": "ok", "last_refreshed_at": 1.0, "errors": {}, "warning": ""}


@pytest.fixture
async def repo(tmp_path: Path):
    """创建隔离数据库仓储。

    参数：
        tmp_path: Path，pytest 临时目录

    返回：
        AsyncIterator[Repo]：已打开的仓储
    """
    db = Database()
    await db.open(tmp_path / "test.db")
    yield Repo(db)
    await db.close()


def _settings(enabled: bool = True, *, custom: dict | None = None) -> Settings:
    """构造含三个预设和可选自定义项的设置。

    参数：
        enabled: bool，研报总开关
        custom: dict | None，可选自定义调度

    返回：
        Settings：测试设置
    """
    schedules = [item.model_dump() for item in ResearchConfig().schedules]
    if custom is not None:
        schedules.append(custom)
    return Settings(research=ResearchConfig(enabled=enabled, schedules=schedules))


def _custom(time_value: str = "12:30", calendar: str = "daily") -> dict:
    """构造合法自定义调度。

    参数：
        time_value: str，UTC+8 执行时刻
        calendar: str，日期规则

    返回：
        dict：自定义调度配置
    """
    return {
        "id": "00000000-0000-4000-8000-000000000001",
        "kind": "fixed_time",
        "enabled": True,
        "time": time_value,
        "calendar": calendar,
    }


def _bj(year: int, month: int, day: int, hour: int, minute: int) -> float:
    """构造 UTC+8 固定时刻时间戳。

    参数：
        year: int，年份
        month: int，月份
        day: int，日期
        hour: int，小时
        minute: int，分钟

    返回：
        float：Unix 秒
    """
    return datetime(year, month, day, hour, minute, tzinfo=BEIJING).timestamp()


@pytest.mark.parametrize(
    ("stamp", "report_type"),
    [
        (_bj(2026, 8, 17, 7, 30), "asia_open"),
        (_bj(2026, 7, 15, 14, 30), "europe_open"),
        (_bj(2026, 1, 15, 15, 30), "europe_open"),
        (_bj(2026, 7, 15, 21, 0), "us_open"),
        (_bj(2026, 1, 15, 22, 0), "us_open"),
        (_bj(2026, 3, 27, 15, 30), "europe_open"),
        (_bj(2026, 3, 30, 14, 30), "europe_open"),
        (_bj(2026, 10, 23, 14, 30), "europe_open"),
        (_bj(2026, 10, 26, 15, 30), "europe_open"),
        (_bj(2026, 3, 6, 22, 0), "us_open"),
        (_bj(2026, 3, 9, 21, 0), "us_open"),
        (_bj(2026, 10, 30, 21, 0), "us_open"),
        (_bj(2026, 11, 2, 22, 0), "us_open"),
    ],
)
async def test_market_presets_fire_at_exact_utc8_minute(repo: Repo, stamp: float, report_type: str):
    """东京、伦敦、纽约预设在开盘前半小时的准确 UTC+8 分钟触发。

    参数：
        repo: Repo，隔离仓储
        stamp: float，目标时间戳
        report_type: str，预期研报类型

    返回：
        None：断言唯一 Agent 调用
    """
    agent = StubAgent()
    scheduler = ResearchScheduler(_settings(), agent, repo, calendar=StubCalendar())
    await scheduler.tick(stamp)
    assert agent.calls == [{"report_type": report_type, "hours": 24}]
    assert agent.round_ids == [None]  # 自动调度不预分配轮次编号（仅手动点火预分配）


async def test_missed_minute_is_not_backfilled(repo: Repo):
    """目标分钟过去后不补跑。

    参数：
        repo: Repo，隔离仓储

    返回：
        None：断言无自动调用
    """
    agent = StubAgent()
    scheduler = ResearchScheduler(_settings(), agent, repo, calendar=StubCalendar())
    await scheduler.tick(_bj(2026, 8, 17, 7, 31))
    assert agent.calls == []


async def test_disabled_locked_or_holiday_skips(repo: Repo):
    """总开关关闭、锁占用和官方休市日都跳过自动执行。

    参数：
        repo: Repo，隔离仓储

    返回：
        None：断言三个场景均不调用 Agent
    """
    target = _bj(2026, 8, 17, 7, 30)
    agent = StubAgent()
    await ResearchScheduler(_settings(False), agent, repo, calendar=StubCalendar()).tick(target)
    holiday = StubCalendar({("XTKS", date(2026, 8, 17))})
    await ResearchScheduler(_settings(), agent, repo, calendar=holiday).tick(target)
    locked = ResearchScheduler(_settings(), agent, repo, calendar=StubCalendar())
    await locked._lock.acquire()
    try:
        await locked.tick(target)
    finally:
        locked._lock.release()
    assert agent.calls == []


async def test_custom_time_honors_selected_calendar(repo: Repo):
    """自定义 UTC+8 时刻仅在所选市场交易日执行，daily 不受市场休市影响。

    参数：
        repo: Repo，隔离仓储

    返回：
        None：断言市场日历与 daily 两种行为
    """
    target = _bj(2026, 8, 17, 12, 30)
    closed = StubCalendar({("XNYS", date(2026, 8, 17))})
    market_agent = StubAgent()
    market = ResearchScheduler(
        _settings(custom=_custom(calendar="XNYS")), market_agent, repo, calendar=closed
    )
    await market.tick(target)
    assert market_agent.calls == []

    daily_agent = StubAgent()
    daily = ResearchScheduler(
        _settings(custom=_custom(calendar="daily")), daily_agent, repo, calendar=closed
    )
    await daily.tick(target)
    assert daily_agent.calls == [
        {"report_type": "00000000-0000-4000-8000-000000000001", "hours": 24}
    ]


@pytest.mark.parametrize("market", ["XTKS", "XLON", "XNYS"])
async def test_custom_market_calendars_skip_their_holiday(repo: Repo, market: str):
    """自定义项选择三种市场日历时，各自休市日均不执行。

    参数：
        repo: Repo，隔离仓储
        market: str，待验证市场代码

    返回：
        None：断言所选市场休市时无调用
    """
    closed = StubCalendar({(market, date(2026, 8, 17))})
    agent = StubAgent()
    scheduler = ResearchScheduler(
        _settings(custom=_custom(calendar=market)), agent, repo, calendar=closed
    )
    await scheduler.tick(_bj(2026, 8, 17, 12, 30))
    assert agent.calls == []


@pytest.mark.parametrize(
    ("time_value", "market", "stamp", "should_run"),
    [
        ("00:30", "XNYS", _bj(2026, 8, 17, 0, 30), False),
        ("00:30", "XNYS", _bj(2026, 8, 22, 0, 30), True),
        ("00:30", "XLON", _bj(2026, 8, 17, 0, 30), False),
        ("23:30", "XTKS", _bj(2026, 8, 21, 23, 30), False),
        ("23:30", "XTKS", _bj(2026, 8, 23, 23, 30), True),
    ],
)
async def test_custom_time_uses_market_local_date(
    repo: Repo, time_value: str, market: str, stamp: float, should_run: bool
):
    """自定义时刻绑定市场日历时按市场当地日期判断交易日，而非北京日期。

    参数：
        repo: Repo，隔离仓储
        time_value: str，UTC+8 执行时刻
        market: str，市场日历代码
        stamp: float，当前时间戳
        should_run: bool，该市场当地日期是否交易日

    返回：
        None：断言 Agent 调用与市场预期一致
    """
    agent = StubAgent()
    scheduler = ResearchScheduler(
        _settings(custom=_custom(time_value=time_value, calendar=market)),
        agent,
        repo,
        calendar=StubCalendar(),
    )
    await scheduler.tick(stamp)
    expected = [{"report_type": "00000000-0000-4000-8000-000000000001", "hours": 24}]
    assert agent.calls == (expected if should_run else [])


@pytest.mark.parametrize(
    ("time_value", "market", "holiday", "stamp", "should_run"),
    [
        ("00:30", "XNYS", date(2026, 8, 17), _bj(2026, 8, 18, 0, 30), False),
        ("00:30", "XNYS", date(2026, 8, 22), _bj(2026, 8, 22, 0, 30), True),
        ("23:30", "XTKS", date(2026, 8, 24), _bj(2026, 8, 24, 23, 30), True),
    ],
)
async def test_custom_market_holiday_uses_market_local_date(
    repo: Repo, time_value: str, market: str, holiday: date, stamp: float, should_run: bool
):
    """休市日按市场当地日期命中：与北京日期错位时仍以市场日期为准。

    参数：
        repo: Repo，隔离仓储
        time_value: str，UTC+8 执行时刻
        market: str，市场日历代码
        holiday: date，市场当地休市日
        stamp: float，当前时间戳
        should_run: bool，该市场当地日期是否交易日

    返回：
        None：断言 Agent 调用与市场预期一致
    """
    agent = StubAgent()
    scheduler = ResearchScheduler(
        _settings(custom=_custom(time_value=time_value, calendar=market)),
        agent,
        repo,
        calendar=StubCalendar({(market, holiday)}),
    )
    await scheduler.tick(stamp)
    expected = [{"report_type": "00000000-0000-4000-8000-000000000001", "hours": 24}]
    assert agent.calls == (expected if should_run else [])


@pytest.mark.parametrize(
    ("time_value", "market", "now_stamp", "expected"),
    [
        ("00:30", "XNYS", _bj(2026, 8, 22, 0, 30), _bj(2026, 8, 25, 0, 30)),
        ("23:30", "XTKS", _bj(2026, 8, 21, 12, 0), _bj(2026, 8, 23, 23, 30)),
    ],
)
async def test_custom_next_run_uses_market_local_date(
    repo: Repo, time_value: str, market: str, now_stamp: float, expected: float
):
    """下一次执行时间按市场当地日期跳过休市日，与到期判断口径一致。

    参数：
        repo: Repo，隔离仓储
        time_value: str，UTC+8 执行时刻
        market: str，市场日历代码
        now_stamp: float，当前时间戳
        expected: float，预期下一次执行时间戳

    返回：
        None：断言状态接口 next_run_at 与预期一致
    """
    scheduler = ResearchScheduler(
        _settings(custom=_custom(time_value=time_value, calendar=market)),
        StubAgent(),
        repo,
        calendar=StubCalendar(),
    )
    status = scheduler.status(now_stamp)
    item = next(i for i in status["items"] if i["id"] == "00000000-0000-4000-8000-000000000001")
    assert item["next_run_at"] == expected


async def test_same_schedule_attempt_is_idempotent(repo: Repo):
    """同一计划日期同一调度即使 Agent 未落报告也只执行一次。

    参数：
        repo: Repo，隔离仓储

    返回：
        None：断言独立调度执行记录阻止重复
    """
    agent = StubAgent()
    scheduler = ResearchScheduler(_settings(), agent, repo, calendar=StubCalendar())
    target = _bj(2026, 8, 17, 7, 30)
    await scheduler.tick(target)
    await scheduler.tick(target)
    assert agent.calls == [{"report_type": "asia_open", "hours": 24}]


async def test_report_completed_after_midnight_does_not_block_next_scheduled_date(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
):
    """前一日任务跨零点完成后，次日相同调度仍可按时执行。

    参数：
        repo: Repo，隔离仓储
        monkeypatch: pytest.MonkeyPatch，固定报告完成时间

    返回：
        None：断言报告完成日期不再承担调度幂等语义
    """
    schedule_id = "00000000-0000-4000-8000-000000000001"
    monkeypatch.setattr("src.memory.research_repo._now", lambda: _bj(2026, 8, 18, 0, 5))
    await repo.research.save_failed_report(
        report_type=schedule_id,
        error="前一日 23:59 任务跨零点完成",
    )
    agent = StubAgent()
    scheduler = ResearchScheduler(
        _settings(custom=_custom(time_value="23:59")),
        agent,
        repo,
        calendar=StubCalendar(),
    )

    await scheduler.tick(_bj(2026, 8, 18, 23, 59))

    assert agent.calls == [{"report_type": schedule_id, "hours": 24}]


async def test_hot_toggle_applies_without_restart(repo: Repo):
    """共享配置总开关原地改为开启后，未来命中分钟立即生效。

    参数：
        repo: Repo，隔离仓储

    返回：
        None：断言热开关读取共享对象
    """
    settings = _settings(False)
    agent = StubAgent()
    scheduler = ResearchScheduler(settings, agent, repo, calendar=StubCalendar())
    await scheduler.tick(_bj(2026, 8, 17, 7, 30))
    settings.research.enabled = True
    await scheduler.tick(_bj(2026, 8, 18, 7, 30))
    assert agent.calls == [{"report_type": "asia_open", "hours": 24}]


async def test_item_toggle_add_and_delete_apply_hot(repo: Repo):
    """单项启停、自定义新增与删除都读取共享列表并立即生效。

    参数：
        repo: Repo，隔离仓储

    返回：
        None：断言无需重启即可改变未来分钟行为
    """
    settings = _settings()
    asia = settings.research.schedules[0]
    asia.enabled = False
    agent = StubAgent()
    scheduler = ResearchScheduler(settings, agent, repo, calendar=StubCalendar())
    await scheduler.tick(_bj(2026, 8, 17, 7, 30))
    asia.enabled = True
    await scheduler.tick(_bj(2026, 8, 18, 7, 30))

    custom = FixedTimeSchedule(**_custom())
    settings.research.schedules.append(custom)
    await scheduler.tick(_bj(2026, 8, 18, 12, 30))
    settings.research.schedules.remove(custom)
    await scheduler.tick(_bj(2026, 8, 19, 12, 30))

    assert [call["report_type"] for call in agent.calls] == [
        "asia_open",
        "00000000-0000-4000-8000-000000000001",
    ]


async def test_accidental_same_minute_collision_runs_only_first(repo: Repo):
    """热变更意外制造同分钟冲突时只执行配置列表首项。

    参数：
        repo: Repo，隔离仓储

    返回：
        None：断言碰撞不会产生两个 LLM 调用
    """
    schedules = [item.model_dump() for item in ResearchConfig().schedules]
    schedules[0]["enabled"] = False
    schedules.append(_custom(time_value="07:30"))
    settings = Settings(research=ResearchConfig(enabled=True, schedules=schedules))
    settings.research.schedules[0].enabled = True
    agent = StubAgent()
    scheduler = ResearchScheduler(settings, agent, repo, calendar=StubCalendar())
    await scheduler.tick(_bj(2026, 8, 17, 7, 30))
    assert agent.calls == [{"report_type": "asia_open", "hours": 24}]


async def test_start_now_returns_immediately_and_runs_in_background(repo: Repo):
    """点火即返回：start_now 返回 started=True 时 Agent 尚未执行，后台任务结束后调用发生。

    参数：
        repo: Repo，隔离仓储

    返回：
        None：断言点火契约与后台任务完成后的 Agent 调用
    """
    agent = StubAgent()
    scheduler = ResearchScheduler(_settings(False), agent, repo, calendar=StubCalendar())
    result = await scheduler.start_now(report_type="event", hours=12)
    round_id = result.pop("round_id")  # 预分配轮次编号：单独断言，余键严格等值
    assert isinstance(round_id, str) and len(round_id) == 32
    assert result == {"started": True, "report_type": "event", "hours": 12}
    assert agent.calls == []  # 点火返回时后台任务尚未执行
    assert scheduler._manual_task is not None
    await asyncio.wait_for(scheduler._manual_task, timeout=1)
    assert agent.calls == [{"report_type": "event", "hours": 12}]
    assert agent.round_ids == [round_id]  # 后台 agent.run 收到同一轮次身份
    assert not scheduler._lock.locked()  # 后台任务收尾释放锁


async def test_start_now_busy_and_no_llm_are_synchronous(repo: Repo):
    """锁占用同步返回 busy、未配置 LLM 同步返回 llm_not_configured，均不点火。

    参数：
        repo: Repo，隔离仓储

    返回：
        None：断言两条同步失败路径不起后台任务
    """
    agent = StubAgent()
    scheduler = ResearchScheduler(_settings(False), agent, repo, calendar=StubCalendar())
    await scheduler._lock.acquire()
    try:
        busy = await scheduler.start_now()
    finally:
        scheduler._lock.release()
    assert busy["started"] is False
    assert busy["error_code"] == "busy"

    agent.llm_configured = False
    no_llm = await scheduler.start_now()
    assert no_llm["started"] is False
    assert no_llm["error_code"] == "llm_not_configured"
    assert scheduler._manual_task is None
    assert agent.calls == []


class BlockingAgent:
    """run 挂起直至测试释放的研报 Agent 边界桩（模拟生成进行中）。"""

    def __init__(self) -> None:
        """初始化调用记录、开始与释放事件。

        参数：无

        返回：
            None：就地初始化事件与已配置 LLM 标记
        """
        self.calls: list[dict] = []
        self.round_ids: list[str | None] = []  # 每次 run 收到的预分配 round_id
        self.llm_configured = True
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled_cleanup = False

    async def run(
        self, report_type: str = "manual", hours: int = 24, *, round_id: str | None = None
    ) -> dict:
        """记录调用、标记开始并挂起，直到测试释放后返回成功结果。

        参数：
            report_type: str，研报类型
            hours: int，回看小时数
            round_id: str | None，调度器手动点火时预分配的审计轮次编号

        返回：
            dict：固定成功结果

        异常：
            asyncio.CancelledError：外部取消时镜像真实 agent 的取消收尾契约
            （此处以标记代副作用）后原样抛出
        """
        self.calls.append({"report_type": report_type, "hours": hours})
        self.round_ids.append(round_id)
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled_cleanup = True  # 镜像真实 agent 的取消收尾契约
            raise
        return {"ok": True, "report_id": 1, "round_id": "r1"}


async def test_start_now_background_survives_caller_cancellation(repo: Repo):
    """取消不变量（断连回归）：点火后取消调用方任务，后台生成仍跑完。

    模拟 HTTP 断连：调用方协程点火后挂起（如保持连接），被取消时后台任务不受影响。

    参数：
        repo: Repo，隔离仓储

    返回：
        None：断言调用方被取消而后台研报执行到底
    """
    agent = BlockingAgent()
    scheduler = ResearchScheduler(_settings(False), agent, repo, calendar=StubCalendar())
    outcome: dict = {}

    async def http_like_call() -> None:
        """模拟请求处理：点火后挂起等待连接生命周期结束。

        参数：无

        返回：
            None：点火结果写入 outcome 后永久挂起，直至被取消
        """
        outcome["result"] = await scheduler.start_now(report_type="event", hours=12)
        await asyncio.Event().wait()  # 模拟响应发送/连接保持：挂起直到断连取消

    request = asyncio.create_task(http_like_call())
    await agent.started.wait()
    fired = dict(outcome["result"])
    round_id = fired.pop("round_id")  # 预分配轮次编号：单独断言，余键严格等值
    assert isinstance(round_id, str) and len(round_id) == 32
    assert fired == {"started": True, "report_type": "event", "hours": 12}
    request.cancel()  # 浏览器断连：请求任务被取消
    with pytest.raises(asyncio.CancelledError):
        await request

    assert scheduler._manual_task is not None and not scheduler._manual_task.done()
    agent.release.set()
    await asyncio.wait_for(scheduler._manual_task, timeout=1)
    assert agent.calls == [{"report_type": "event", "hours": 12}]
    assert agent.round_ids == [round_id]  # 断连后后台任务仍持同一轮次身份跑完
    assert not scheduler._lock.locked()


async def test_shutdown_cancels_running_manual_task(repo: Repo, caplog: pytest.LogCaptureFixture):
    """shutdown 取消进行中的后台任务：任务以取消态收尾、释放锁且无未捕获噪音。

    参数：
        repo: Repo，隔离仓储
        caplog: pytest.LogCaptureFixture，日志捕获夹具

    返回：
        None：断言任务呈取消态、取消抵达 agent 且收尾在 shutdown 返回前完成、锁释放
    """
    agent = BlockingAgent()
    scheduler = ResearchScheduler(_settings(False), agent, repo, calendar=StubCalendar())
    fired = await scheduler.start_now()
    await agent.started.wait()
    task = scheduler._manual_task
    assert task is not None

    with caplog.at_level("INFO", logger="src.research.scheduler"):
        await scheduler.shutdown()

    assert task.done()
    assert task.cancelled()  # 取消语义保留：_run_manual 原样传播，shutdown gather 取回
    assert agent.cancelled_cleanup  # 取消抵达 agent.run，且其收尾在 shutdown 返回前完成
    assert not scheduler._lock.locked()
    assert "手动研报后台任务被取消" in caplog.text
    # 补记钉住：BlockingAgent 桩不触库（无审计轮、无报告），shutdown 补记唯一的取消终态；
    # 生产真实 agent 首个 DB 写即 begin_round，补记的审计轮判重闸会拦截、不会走到这
    reports, total = await repo.research.list_reports_page(10, 0)
    assert total == 1
    assert reports[0].error == "手动研报在开始执行前被关机取消"
    assert reports[0].round_id == fired["round_id"]
    found = await repo.research.find_report_by_round_id(fired["round_id"])
    assert found is not None and found.id == reports[0].id
    await scheduler.shutdown()  # 幂等：无进行中任务时立即返回


async def test_shutdown_before_manual_task_first_execution(repo: Repo):
    """点火后不等待任何执行立即 shutdown：锁从未持有、预留由 done 回调清理，且补记取消终态。

    后台任务在首次执行前被取消时协程体不进入；旧实现锁由调用方任务持有后跨任务转移，
    该窗口下 finally 不执行 → 锁永久 locked、之后点火永远 busy。此外 begin_round 从未
    运行、agent 未留痕，shutdown 须为预分配轮次补写一条「关机取消」失败报告；
    正常执行完的轮 shutdown 早退，不产生额外记录。

    参数：
        repo: Repo，隔离仓储

    返回：
        None：断言任务取消态、锁未持有、预留清除、补记取消终态、再次点火正常
    """
    agent = StubAgent()
    scheduler = ResearchScheduler(_settings(False), agent, repo, calendar=StubCalendar())
    result = await scheduler.start_now()
    assert result["started"] is True
    assert scheduler._manual_reserved is True  # 点火即置预留
    second = await scheduler.start_now()  # 预留期间再点火：同步 busy，不排队
    assert second["started"] is False and second["error_code"] == "busy"

    await scheduler.shutdown()

    task = scheduler._manual_task
    assert task is not None and task.cancelled()  # 首次执行前被取消，协程体未进入
    assert agent.calls == []
    assert not scheduler._lock.locked()  # 锁从未持有，无泄漏
    assert scheduler._manual_reserved is False  # done 回调清理预留
    # 关机补记取消终态：预分配轮次留一条失败报告（begin_round 从未运行，agent 未留痕）
    reports, total = await repo.research.list_reports_page(10, 0)
    assert total == 1
    assert reports[0].error == "手动研报在开始执行前被关机取消"
    assert reports[0].round_id == result["round_id"]
    assert reports[0].report_type == "manual"
    again = await scheduler.start_now()  # 预留已清：可再次正常点火
    assert again["started"] is True
    await asyncio.wait_for(scheduler._manual_task, timeout=1)
    assert agent.calls == [{"report_type": "manual", "hours": 24}]
    assert not scheduler._lock.locked()
    # 正常执行完的轮（StubAgent 不落库）：任务已 done，shutdown 早退不产生额外记录
    await scheduler.shutdown()
    reports, total = await repo.research.list_reports_page(10, 0)
    assert total == 1


async def test_manual_task_completion_releases_lock_and_reservation(repo: Repo):
    """点火后等任务真正跑完：锁释放、预留清除。

    参数：
        repo: Repo，隔离仓储

    返回：
        None：断言后台任务完成后 Agent 调用发生且锁与预留均已释放
    """
    agent = StubAgent()
    scheduler = ResearchScheduler(_settings(False), agent, repo, calendar=StubCalendar())
    await scheduler.start_now(report_type="event", hours=12)
    assert scheduler._manual_reserved is True
    await asyncio.wait_for(scheduler._manual_task, timeout=1)
    assert agent.calls == [{"report_type": "event", "hours": 12}]
    assert not scheduler._lock.locked()
    assert scheduler._manual_reserved is False


class HangingResearchProvider:
    """chat 挂起的 provider 桩（模拟真实 ResearchAgent 生成进行中，供 shutdown 取消回归）。"""

    def __init__(self) -> None:
        """初始化进入事件。

        参数：无

        返回：
            None：就地初始化 entered 事件，供测试等待 chat 真正开始
        """
        self.entered = asyncio.Event()

    async def chat(self, system: str, messages: list[dict], tools: list[dict]) -> dict:
        """标记进入后挂起，直至任务被取消（永不正常返回）。

        参数：
            system: str，传给 LLM 的系统提示词
            messages: list[dict]，传给 LLM 的消息历史
            tools: list[dict]，传给 LLM 的工具定义

        返回：
            dict，永不返回；挂起只能被外部取消打断
        """
        self.entered.set()
        await asyncio.sleep(60)  # 远超测试等待，仅取消可打断

    def tool_result_message(self, call: object, result: str) -> dict:
        """把工具调用结果封装为模拟提供商消息（本桩不会走到）。

        参数：
            call: object，待封装的工具调用
            result: str，工具执行结果文本

        返回：
            dict，包含 role=tool 与结果内容的工具消息
        """
        return {"role": "tool", "content": result}


async def test_shutdown_cancel_finishes_real_agent_cleanup(repo: Repo, tmp_path: Path):
    """shutdown 取消进行中手动研报：真实 ResearchAgent 完成取消收尾（预分配轮终态唯一）。

    全链路：start_now 点火 → provider 挂起（LLM 调用进行中）→ shutdown 取消 →
    断言 ①agent 取消收尾落唯一失败报告（error 含 CancelledError，round_id 为预分配值）、
    ②审计轮以 error 闭合、③事件序列以 research_round ok=False 收尾；
    begin_round 已跑，shutdown 补记的审计轮判重闸拦截、不产生第二条记录。

    参数：
        repo: Repo，隔离仓储
        tmp_path: Path，pytest 临时目录夹具

    返回：
        None，断言取消态与失败收尾副作用同时成立且无补记双写
    """
    events: list[dict] = []
    settings = _settings(False)
    provider = HangingResearchProvider()
    agent = ResearchAgent(
        settings=settings,
        provider=provider,
        repo=repo,
        audit=AuditTrail(repo, AuditConfig(dir=str(tmp_path / "audit"))),
        prompt_loader=ResearchPromptLoader(tmp_path / "research_prompt.md"),
        data_provider=ResearchDataProvider(),  # 空装配：预注入优雅降级，chat 前不触网
        watchlist=("BTC_USDT",),
        notify_event=events.append,
        max_turns=10,
        timeout_seconds=60,
    )
    scheduler = ResearchScheduler(settings, agent, repo, calendar=StubCalendar())
    fired = await scheduler.start_now()
    await asyncio.wait_for(provider.entered.wait(), timeout=1)
    task = scheduler._manual_task
    assert task is not None

    await scheduler.shutdown()

    assert task.cancelled()
    assert not scheduler._lock.locked()
    reports, total = await repo.research.list_reports_page(10, 0)
    assert total == 1  # agent 取消收尾落唯一失败报告；补记被判重闸拦截，无双写
    assert reports[0].error == "CancelledError: 研报被取消"
    assert reports[0].round_id == fired["round_id"]
    round_row = await repo.get_audit_round(fired["round_id"])
    assert round_row is not None and round_row.ended_at is not None
    assert round_row.error == "CancelledError: 研报被取消"
    assert [e["type"] for e in events] == ["research_round_start", "research_round"]
    assert events[-1]["data"] == {"round_id": round_row.round_id, "ok": False}


async def test_schedule_status_exposes_next_run_and_calendar(repo: Repo):
    """调度状态返回总开关、各项下一次 UTC+8 时间和日历健康状态。

    参数：
        repo: Repo，隔离仓储

    返回：
        None：断言前端状态接口所需字段
    """
    scheduler = ResearchScheduler(_settings(), StubAgent(), repo, calendar=StubCalendar())
    status = scheduler.status(_bj(2026, 8, 17, 6, 0))
    assert status["enabled"] is True
    assert status["calendar"]["state"] == "ok"
    asia = next(item for item in status["items"] if item["id"] == "asia_open")
    assert asia["next_run_at"] == _bj(2026, 8, 17, 7, 30)


class BlockingResearchHistory:
    """在幂等查询中挂起，用于稳定复现调度并发窗口。"""

    def __init__(self) -> None:
        """创建进入与释放两个同步事件。

        参数：无

        返回：
            None：就地初始化事件
        """
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def claim_schedule_run(self, _schedule_id: str, _scheduled_date: date) -> bool:
        """通知查询已开始并等待测试释放。

        参数：
            _schedule_id: str，调度标识（本桩不使用）
            _scheduled_date: date，计划日期（本桩不使用）

        返回：
            bool：固定返回 True，模拟首次认领成功
        """
        self.entered.set()
        await self.release.wait()
        return True


class BlockingRepo:
    """只暴露调度器需要的 research 幂等仓储。"""

    def __init__(self, history: BlockingResearchHistory) -> None:
        """保存可控幂等仓储。

        参数：
            history: BlockingResearchHistory，可控查询边界

        返回：
            None：就地设置 research 属性
        """
        self.research = history


async def test_auto_tick_claims_execution_before_first_await():
    """自动任务命中后先取得执行权，手动任务不得抢锁后让自动任务排队补跑。

    参数：无

    返回：
        None：断言手动入口返回 busy，自动项只执行一次
    """
    history = BlockingResearchHistory()
    agent = StubAgent()
    scheduler = ResearchScheduler(
        _settings(),
        agent,
        BlockingRepo(history),
        calendar=StubCalendar(),  # type: ignore[arg-type]
    )
    automatic = asyncio.create_task(scheduler.tick(_bj(2026, 8, 17, 7, 30)))
    await history.entered.wait()

    manual = await scheduler.start_now()
    history.release.set()
    await automatic

    assert manual["error_code"] == "busy"
    assert agent.calls == [{"report_type": "asia_open", "hours": 24}]


async def test_hot_disabled_item_is_rechecked_before_agent_call():
    """幂等查询期间热停用命中项后，本轮必须放弃且不得按旧快照执行。

    参数：无

    返回：
        None：断言热停用后没有 Agent 调用
    """
    history = BlockingResearchHistory()
    settings = _settings()
    agent = StubAgent()
    scheduler = ResearchScheduler(
        settings,
        agent,
        BlockingRepo(history),
        calendar=StubCalendar(),  # type: ignore[arg-type]
    )
    automatic = asyncio.create_task(scheduler.tick(_bj(2026, 8, 17, 7, 30)))
    await history.entered.wait()
    settings.research.schedules[0].enabled = False
    history.release.set()
    await automatic

    assert agent.calls == []


async def test_run_forever_refreshes_on_start_and_aligns_natural_minute(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
):
    """长期循环启动先刷新日历，随后睡眠到下一个自然分钟边界。

    参数：
        repo: Repo，隔离仓储
        monkeypatch: pytest.MonkeyPatch，替换时钟和睡眠边界

    返回：
        None：断言启动刷新次数和 30.01 秒对齐延迟
    """
    calendar = StubCalendar()
    scheduler = ResearchScheduler(_settings(False), StubAgent(), repo, calendar=calendar)
    delays: list[float] = []

    class StopLoop(Exception):
        """终止长期循环的测试专用异常。"""

    async def stop_after_first_sleep(delay: float) -> None:
        """记录首次睡眠并终止循环。

        参数：
            delay: float，调度器计算的等待秒数

        返回：
            None：此桩始终抛错，不会正常返回

        异常：
            StopLoop：记录延迟后固定抛出
        """
        delays.append(delay)
        raise StopLoop

    monkeypatch.setattr("src.research.scheduler.time.time", lambda: 90.0)
    monkeypatch.setattr("src.research.scheduler.asyncio.sleep", stop_after_first_sleep)
    with pytest.raises(StopLoop):
        await scheduler.run_forever()
    assert calendar.refresh_calls == 1
    assert delays == [pytest.approx(30.01)]


@pytest.mark.parametrize(
    ("start_stamp", "target_stamp", "target_time"),
    [
        (_bj(2026, 8, 17, 0, 9) + 30, _bj(2026, 8, 17, 0, 10), "00:10"),
        (_bj(2026, 8, 17, 0, 10), _bj(2026, 8, 17, 0, 15), "00:15"),
    ],
)
async def test_failed_refresh_recovers_before_same_day_schedule(
    repo: Repo,
    monkeypatch: pytest.MonkeyPatch,
    start_stamp: float,
    target_stamp: float,
    target_time: str,
):
    """刷新失败后同日按退避重试，并在目标任务判断前恢复日历。

    参数：
        repo: Repo，隔离仓储
        monkeypatch: pytest.MonkeyPatch，控制调度循环时钟与睡眠
        start_stamp: float，首次刷新与巡检时刻
        target_stamp: float，下一次循环推进到的目标时刻
        target_time: str，自定义调度的 UTC+8 时间

    返回：
        None：断言第二次刷新先于 00:10 调度并允许任务执行
    """

    class RecoveringCalendar(StubCalendar):
        """第一次刷新失败、第二次成功后才确认交易日的日历桩。"""

        def __init__(self) -> None:
            """初始化为不可确认交易日。

            参数：无

            返回：
                None：就地初始化失败次数和开市状态
            """
            super().__init__()
            self.ready = False

        async def refresh(self) -> CalendarRefreshResult:
            """首次返回失败，第二次恢复完整来源与缓存。

            参数：无

            返回：
                CalendarRefreshResult：本次刷新结果
            """
            self.refresh_calls += 1
            if self.refresh_calls == 1:
                return CalendarRefreshResult((), {"XNYS": "network down"}, False)
            self.ready = True
            return CalendarRefreshResult(("XTKS", "XLON", "XNYS"), {}, True)

        def is_trading_day(self, market: str, target: date) -> bool:
            """仅在日历恢复后确认工作日开市。

            参数：
                market: str，市场代码
                target: date，待判断日期

            返回：
                bool：恢复后工作日为 True
            """
            return self.ready and target.weekday() < 5

    class StopLoop(Exception):
        """完成目标分钟巡检后终止长期循环。"""

    state = {"stamp": start_stamp, "sleeps": 0}

    class FakeDateTime(datetime):
        """从可变测试状态返回 UTC+8 当前时刻。"""

        @classmethod
        def now(cls, tz=None):
            """返回测试状态中的当前时刻。

            参数：
                tz: tzinfo | None，目标时区

            返回：
                datetime：测试状态对应时间
            """
            return datetime.fromtimestamp(state["stamp"], tz)

    async def advance_to_target(_delay: float) -> None:
        """首次睡眠推进到 00:10，第二次终止循环。

        参数：
            _delay: float，调度器计算的睡眠秒数

        返回：
            None：首次调用只更新时间

        异常：
            StopLoop：第二次调用时终止循环
        """
        state["sleeps"] += 1
        if state["sleeps"] == 1:
            state["stamp"] = target_stamp
            return
        raise StopLoop

    calendar = RecoveringCalendar()
    agent = StubAgent()
    scheduler = ResearchScheduler(
        _settings(custom=_custom(time_value=target_time, calendar="XTKS")),
        agent,
        repo,
        calendar=calendar,
    )
    monkeypatch.setattr("src.research.scheduler.datetime", FakeDateTime)
    monkeypatch.setattr("src.research.scheduler.time.time", lambda: state["stamp"])
    monkeypatch.setattr("src.research.scheduler.asyncio.sleep", advance_to_target)

    with pytest.raises(StopLoop):
        await scheduler.run_forever()

    assert calendar.refresh_calls == 2
    assert agent.calls == [{"report_type": "00000000-0000-4000-8000-000000000001", "hours": 24}]
