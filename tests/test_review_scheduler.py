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
        self.calls: list[tuple[float, float]] = []

    async def run(self, period_start: float, period_end: float) -> dict:
        self.calls.append((period_start, period_end))
        return {"ok": True, "report_id": 1}


@pytest.fixture
async def repo(tmp_path):
    db = Database()
    await db.open(tmp_path / "test.db")
    yield Repo(db)
    await db.close()


def _settings(enabled: bool = True, interval_days: int = 1) -> Settings:
    return Settings(
        review=ReviewConfig(enabled=enabled, daily_time="03:00", interval_days=interval_days)
    )


def _anchors() -> tuple[float, float]:
    """当日00:00 与 03:00（触发时刻）。与实现走同一对纯函数，期望区间手工推导。"""
    now = time.time()
    return local_day_start(now), daily_fire_ts("03:00", now)


async def test_tick_fires_after_daily_time(repo):
    """到点且从未复盘（latest None）→ 触发「昨日00:00 ~ 当日00:00」区间。"""
    agent = StubAgent()
    scheduler = ReviewScheduler(_settings(), agent, repo)
    day_start, fire = _anchors()
    await scheduler._tick(now=fire + 1)
    assert agent.calls == [(day_start - 86400, day_start)]


async def test_tick_skips_before_daily_time(repo):
    agent = StubAgent()
    scheduler = ReviewScheduler(_settings(), agent, repo)
    day_start, fire = _anchors()
    await scheduler._tick(now=fire - 1)  # 02:59:59 未到点
    assert agent.calls == []
    assert fire - day_start == 3 * 3600  # 触发时刻确为当日 03:00


async def test_tick_skips_when_already_reviewed(repo):
    """当日已跑（latest=当日00:00）→ 不重复触发（重启幂等以落库为准）。"""
    agent = StubAgent()
    scheduler = ReviewScheduler(_settings(), agent, repo)
    day_start, fire = _anchors()
    await repo.review.save_review_report(day_start - 86400, day_start, "{}", "", "none")
    await scheduler._tick(now=fire + 1)
    assert agent.calls == []


async def test_tick_skips_when_disabled(repo):
    agent = StubAgent()
    scheduler = ReviewScheduler(_settings(enabled=False), agent, repo)
    _, fire = _anchors()
    await scheduler._tick(now=fire + 1)
    assert agent.calls == []


async def test_tick_skips_when_locked(repo):
    """锁被占用（手动触发进行中）→ 巡检跳过，下一分钟再试。"""
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
    """agent 回报 llm_not_configured（复盘未实际开始）→ run_now 包装 started=False（语义诚实）。"""

    class _NoLlmAgent:
        async def run(self, period_start: float, period_end: float) -> dict:
            return {"ok": False, "error": "任意错误文案", "error_code": "llm_not_configured"}

    scheduler = ReviewScheduler(_settings(), _NoLlmAgent(), repo)
    result = await scheduler.run_now()
    assert result["started"] is False
    assert result["error_code"] == "llm_not_configured"


async def test_run_now_runs_yesterday_period(repo):
    agent = StubAgent()
    scheduler = ReviewScheduler(_settings(), agent, repo)
    day_start = local_day_start(time.time())
    result = await scheduler.run_now()
    assert result["started"] is True and result["ok"] is True
    assert agent.calls == [(day_start - 86400, day_start)]


# ---------- 人工补跑指定区间 ----------


async def test_run_now_with_explicit_period_passthrough(repo):
    """人工补跑：指定区间原样透传到 agent.run（不走昨日区间）。"""
    agent = StubAgent()
    scheduler = ReviewScheduler(_settings(), agent, repo)
    result = await scheduler.run_now(period_start=1000.0, period_end=2000.0)
    assert result["started"] is True and result["ok"] is True
    assert agent.calls == [(1000.0, 2000.0)]


async def test_run_now_invalid_period(repo):
    """非法区间（start>=end、非数字、只给一端、bool）→ started=False +
    error_code=invalid_period（server 层映 422），不触发 agent。"""
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
    """interval_days=3：距上次复盘仅 2 天 → 未满间隔，不触发。"""
    agent = StubAgent()
    scheduler = ReviewScheduler(_settings(interval_days=3), agent, repo)
    day_start, fire = _anchors()
    await repo.review.save_review_report(
        day_start - 5 * 86400, day_start - 2 * 86400, "{}", "", "none"
    )
    await scheduler._tick(now=fire + 1)
    assert agent.calls == []


async def test_tick_interval_reached_fires_span(repo):
    """interval_days=3：距上次复盘恰满 3 天 → 触发，且区间为最近 3 天。"""
    agent = StubAgent()
    scheduler = ReviewScheduler(_settings(interval_days=3), agent, repo)
    day_start, fire = _anchors()
    await repo.review.save_review_report(
        day_start - 6 * 86400, day_start - 3 * 86400, "{}", "", "none"
    )
    await scheduler._tick(now=fire + 1)
    assert agent.calls == [(day_start - 3 * 86400, day_start)]


async def test_tick_interval_first_run_uses_span(repo):
    """interval_days=3 且从未复盘（latest None）→ 首次即按 3 天区间触发。"""
    agent = StubAgent()
    scheduler = ReviewScheduler(_settings(interval_days=3), agent, repo)
    day_start, fire = _anchors()
    await scheduler._tick(now=fire + 1)
    assert agent.calls == [(day_start - 3 * 86400, day_start)]


async def test_run_now_uses_interval_span(repo):
    """run_now 无参：默认区间同步为最近 interval_days 天。"""
    agent = StubAgent()
    scheduler = ReviewScheduler(_settings(interval_days=3), agent, repo)
    day_start = local_day_start(time.time())
    result = await scheduler.run_now()
    assert result["started"] is True and result["ok"] is True
    assert agent.calls == [(day_start - 3 * 86400, day_start)]
