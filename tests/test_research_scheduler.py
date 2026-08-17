"""研报严格分钟调度、市场时区、交易日与手动触发测试。"""

import asyncio

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src.config import FixedTimeSchedule, ResearchConfig, Settings
from src.memory import Database, Repo
from src.research.scheduler import ResearchScheduler

BEIJING = ZoneInfo("Asia/Shanghai")


class StubAgent:
    """记录调用并返回固定成功结果的研报 Agent 边界桩。"""

    def __init__(self) -> None:
        """初始化空调用记录。

        参数：无

        返回：
            None：就地创建调用列表
        """
        self.calls: list[dict] = []

    async def run(self, report_type: str = "manual", hours: int = 24) -> dict:
        """记录报告类型和回看小时并返回成功结果。

        参数：
            report_type: str，研报类型
            hours: int，回看小时数

        返回：
            dict：固定成功结果
        """
        self.calls.append({"report_type": report_type, "hours": hours})
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

    async def refresh(self) -> None:
        """模拟官方日历刷新成功。

        参数：无

        返回：
            None：刷新计数加一
        """
        self.refresh_calls += 1

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


async def test_run_now_busy_success_and_no_llm(repo: Repo):
    """手动触发保持忙、成功与 LLM 未配置三种公开结果。

    参数：
        repo: Repo，隔离仓储

    返回：
        None：断言手动入口兼容原契约
    """
    agent = StubAgent()
    scheduler = ResearchScheduler(_settings(False), agent, repo, calendar=StubCalendar())
    result = await scheduler.run_now(report_type="event", hours=12)
    assert result["started"] is True
    assert agent.calls == [{"report_type": "event", "hours": 12}]

    await scheduler._lock.acquire()
    try:
        assert (await scheduler.run_now())["error_code"] == "busy"
    finally:
        scheduler._lock.release()

    class NoLlmAgent:
        """返回 LLM 未配置结果的边界桩。"""

        async def run(self, report_type: str = "manual", hours: int = 24) -> dict:
            """返回结构化 LLM 未配置失败。

            参数：
                report_type: str，研报类型
                hours: int，回看小时数

            返回：
                dict：llm_not_configured 失败结果
            """
            return {"ok": False, "error": "未配置", "error_code": "llm_not_configured"}

    no_llm = ResearchScheduler(_settings(), NoLlmAgent(), repo, calendar=StubCalendar())
    assert (await no_llm.run_now())["started"] is False


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

    manual = await scheduler.run_now()
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
