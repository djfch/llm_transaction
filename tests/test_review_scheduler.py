"""src/review/scheduler.py 测试：时间逻辑走可注入 now 的 _tick 与纯函数，不 sleep 60s。

覆盖：到点触发（enabled 且 latest None）、间隔内不重复（latest 距今不足 interval_days）、
disabled 跳过、未到点跳过、锁占用时巡检跳过、run_now 持锁返回「复盘进行中」、
run_now 正常触发最近 interval_days 天区间（默认 1 天）。
"""

import time

import pytest

from src.config import ReviewConfig, Settings
from src.memory import Database, Repo
from src.review.scheduler import ReviewScheduler, daily_fire_ts, local_day_start


class StubAgent:
    """记录调用区间并返回固定结果的 stub（鸭子类型替代 ReviewAgent）。"""

    def __init__(self) -> None:
        """初始化空调用记录列表。

        参数：无

        返回：
            None，就地初始化 calls 为空列表，供后续断言调用区间
        """
        self.calls: list[tuple[float, float]] = []

    async def run(self, period_start: float, period_end: float) -> dict:
        """记录本次调用区间并返回固定的成功结果。

        参数：
            period_start: float，复盘区间起点（Unix 时间戳）
            period_end: float，复盘区间终点（Unix 时间戳）

        返回：
            dict：固定为 {"ok": True, "report_id": 1} 的成功结果
        """
        self.calls.append((period_start, period_end))
        return {"ok": True, "report_id": 1}


@pytest.fixture
async def repo(tmp_path):
    """构造指向临时数据库的 Repo 实例，测试结束后关闭数据库。

    参数：
        tmp_path: Path，pytest 临时目录夹具，测试数据库文件落在其中

    返回：
        AsyncIterator[Repo]，yield 已打开临时数据库的仓储对象，测试结束后关闭连接
    """
    db = Database()
    await db.open(tmp_path / "test.db")
    yield Repo(db)
    await db.close()


def _settings(enabled: bool = True, interval_days: int = 1) -> Settings:
    """构造复盘调度测试用的 Settings（daily_time 固定为 03:00）。

    参数：
        enabled: bool，复盘功能开关，默认开启
        interval_days: int，复盘间隔天数，默认 1 天

    返回：
        Settings：仅定制复盘配置、其余走默认的应用配置对象
    """
    return Settings(
        review=ReviewConfig(enabled=enabled, daily_time="03:00", interval_days=interval_days)
    )


def _anchors() -> tuple[float, float]:
    """当日00:00 与 03:00（触发时刻）。与实现走同一对纯函数，期望区间手工推导。

    参数：
        无

    返回：
        tuple[float, float]：当日本地零点与 03:00 触发时刻的时间戳
    """
    now = time.time()
    return local_day_start(now), daily_fire_ts("03:00", now)


async def test_tick_fires_after_daily_time(repo):
    """到点且从未复盘（latest None）→ 触发「昨日00:00 ~ 当日00:00」区间。

    参数：
        repo: Repo，临时数据库仓储夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    agent = StubAgent()
    scheduler = ReviewScheduler(_settings(), agent, repo)
    day_start, fire = _anchors()
    await scheduler._tick(now=fire + 1)
    assert agent.calls == [(day_start - 86400, day_start)]


async def test_tick_skips_before_daily_time(repo):
    """未到当日触发时刻（02:59:59）→ 巡检不触发复盘，agent 零调用。

    参数：
        repo: Repo，临时数据库仓储夹具，提供空的复盘报告存储

    返回：
        None，断言 agent.calls 为空，且触发时刻确为当日 03:00
    """
    agent = StubAgent()
    scheduler = ReviewScheduler(_settings(), agent, repo)
    day_start, fire = _anchors()
    await scheduler._tick(now=fire - 1)  # 02:59:59 未到点
    assert agent.calls == []
    assert fire - day_start == 3 * 3600  # 触发时刻确为当日 03:00


async def test_tick_skips_when_already_reviewed(repo):
    """当日已跑（latest=当日00:00）→ 不重复触发（重启幂等以落库为准）。

    参数：
        repo: Repo，临时数据库仓储夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    agent = StubAgent()
    scheduler = ReviewScheduler(_settings(), agent, repo)
    day_start, fire = _anchors()
    await repo.review.save_review_report(day_start - 86400, day_start, "{}", "", "none")
    await scheduler._tick(now=fire + 1)
    assert agent.calls == []


async def test_tick_skips_when_disabled(repo):
    """复盘功能关闭（enabled=False）→ 即使已过触发时刻也不触发，agent 零调用。

    参数：
        repo: Repo，临时数据库仓储夹具，提供空的复盘报告存储

    返回：
        None，断言 agent.calls 为空（disabled 时 _tick 直接跳过）
    """
    agent = StubAgent()
    scheduler = ReviewScheduler(_settings(enabled=False), agent, repo)
    _, fire = _anchors()
    await scheduler._tick(now=fire + 1)
    assert agent.calls == []


async def test_tick_skips_when_locked(repo):
    """锁被占用（手动触发进行中）→ 巡检跳过，下一分钟再试。

    参数：
        repo: Repo，临时数据库仓储夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    agent = StubAgent()
    scheduler = ReviewScheduler(_settings(), agent, repo)
    _, fire = _anchors()
    await scheduler._lock.acquire()
    try:
        await scheduler._tick(now=fire + 1)
    finally:
        scheduler._lock.release()
    assert agent.calls == []


async def test_run_now_busy_when_locked(repo):
    """调度锁被占用时 run_now 不等待、立即返回 busy，不触发 agent。

    参数：
        repo: Repo，临时数据库仓储夹具，提供复盘报告存储

    返回：
        None，断言 started=False、error_code=busy、错误文案非空且 agent 零调用
    """
    agent = StubAgent()
    scheduler = ReviewScheduler(_settings(), agent, repo)
    await scheduler._lock.acquire()
    try:
        result = await scheduler.run_now()
    finally:
        scheduler._lock.release()
    assert result["started"] is False
    assert result["error_code"] == "busy"  # server 层据此映 409（不判中文文案）
    assert result["error"]  # 文案非空即可
    assert agent.calls == []


async def test_run_now_llm_not_configured_started_false(repo):
    """agent 回报 llm_not_configured（复盘未实际开始）→ run_now 包装 started=False（语义诚实）。

    参数：
        repo: Repo，临时数据库仓储夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """

    class _NoLlmAgent:
        async def run(self, period_start: float, period_end: float) -> dict:
            """返回 llm_not_configured 失败结果，模拟未配置 LLM 的复盘 agent。

            参数：
                period_start: float，复盘区间起点（本桩不使用）
                period_end: float，复盘区间终点（本桩不使用）

            返回：
                dict：ok=False 且 error_code=llm_not_configured 的失败结果
            """
            return {"ok": False, "error": "任意错误文案", "error_code": "llm_not_configured"}

    scheduler = ReviewScheduler(_settings(), _NoLlmAgent(), repo)
    result = await scheduler.run_now()
    assert result["started"] is False
    assert result["error_code"] == "llm_not_configured"


async def test_run_now_runs_yesterday_period(repo):
    """run_now 无参触发：默认跑「昨日00:00 ~ 当日00:00」区间并正常完成。

    参数：
        repo: Repo，临时数据库仓储夹具，提供复盘报告存储

    返回：
        None，断言 started=True、ok=True 且 agent 收到昨日区间调用
    """
    agent = StubAgent()
    scheduler = ReviewScheduler(_settings(), agent, repo)
    day_start = local_day_start(time.time())
    result = await scheduler.run_now()
    assert result["started"] is True and result["ok"] is True
    assert agent.calls == [(day_start - 86400, day_start)]


# ---------- 人工补跑指定区间 ----------


async def test_run_now_with_explicit_period_passthrough(repo):
    """人工补跑：指定区间原样透传到 agent.run（不走昨日区间）。

    参数：
        repo: Repo，临时数据库仓储夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    agent = StubAgent()
    scheduler = ReviewScheduler(_settings(), agent, repo)
    result = await scheduler.run_now(period_start=1000.0, period_end=2000.0)
    assert result["started"] is True and result["ok"] is True
    assert agent.calls == [(1000.0, 2000.0)]


async def test_run_now_invalid_period(repo):
    """非法区间（start>=end、非数字、只给一端、bool）→ started=False +
    error_code=invalid_period（server 层映 422），不触发 agent。

    参数：
        repo: Repo，临时数据库仓储夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    agent = StubAgent()
    scheduler = ReviewScheduler(_settings(), agent, repo)
    bad_periods = [
        (2000.0, 1000.0),
        (1000.0, 1000.0),
        ("x", 2000.0),
        (1000.0, None),
        (True, 2000.0),
    ]
    for start, end in bad_periods:
        result = await scheduler.run_now(period_start=start, period_end=end)
        assert result["started"] is False, (start, end)
        assert result["error_code"] == "invalid_period", (start, end)
    assert agent.calls == []


# ---------- interval_days：指定间隔天数复盘 ----------


async def test_tick_interval_not_reached_skips(repo):
    """interval_days=3：距上次复盘仅 2 天 → 未满间隔，不触发。

    参数：
        repo: Repo，临时数据库仓储夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    agent = StubAgent()
    scheduler = ReviewScheduler(_settings(interval_days=3), agent, repo)
    day_start, fire = _anchors()
    await repo.review.save_review_report(
        day_start - 5 * 86400, day_start - 2 * 86400, "{}", "", "none"
    )
    await scheduler._tick(now=fire + 1)
    assert agent.calls == []


async def test_tick_latest_not_day_aligned(repo):
    """人工补跑的 period_end 非日对齐（昨日 12:00）：幂等判定按自然日计，今日照常触发。

    参数：
        repo: Repo，临时数据库仓储夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    agent = StubAgent()
    scheduler = ReviewScheduler(_settings(), agent, repo)
    day_start, fire = _anchors()
    await repo.review.save_review_report(
        day_start - 86400, day_start - 43200, "{}", "", "none"
    )  # 昨日 12:00 结束的补跑区间
    await scheduler._tick(now=fire + 1)
    assert agent.calls == [(day_start - 86400, day_start)]


async def test_tick_interval_reached_fires_span(repo):
    """interval_days=3：距上次复盘恰满 3 天 → 触发，且区间为最近 3 天。

    参数：
        repo: Repo，临时数据库仓储夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    agent = StubAgent()
    scheduler = ReviewScheduler(_settings(interval_days=3), agent, repo)
    day_start, fire = _anchors()
    await repo.review.save_review_report(
        day_start - 6 * 86400, day_start - 3 * 86400, "{}", "", "none"
    )
    await scheduler._tick(now=fire + 1)
    assert agent.calls == [(day_start - 3 * 86400, day_start)]


async def test_tick_interval_first_run_uses_span(repo):
    """interval_days=3 且从未复盘（latest None）→ 首次即按 3 天区间触发。

    参数：
        repo: Repo，临时数据库仓储夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    agent = StubAgent()
    scheduler = ReviewScheduler(_settings(interval_days=3), agent, repo)
    day_start, fire = _anchors()
    await scheduler._tick(now=fire + 1)
    assert agent.calls == [(day_start - 3 * 86400, day_start)]


async def test_run_now_uses_interval_span(repo):
    """run_now 无参：默认区间同步为最近 interval_days 天。

    参数：
        repo: Repo，临时数据库仓储夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    agent = StubAgent()
    scheduler = ReviewScheduler(_settings(interval_days=3), agent, repo)
    day_start = local_day_start(time.time())
    result = await scheduler.run_now()
    assert result["started"] is True and result["ok"] is True
    assert agent.calls == [(day_start - 3 * 86400, day_start)]
