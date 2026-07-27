"""src/review/scheduler.py 测试：时间逻辑走可注入 now 的 _tick 与纯函数，不 sleep 60s。

覆盖：到点触发（enabled 且 latest None）、当日已跑不重复（latest=当日00:00）、
disabled 跳过、未到点跳过、锁占用时巡检跳过、run_now 持锁返回「复盘进行中」、
run_now 正常触发昨日区间。
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


def _settings(enabled: bool = True) -> Settings:
    return Settings(review=ReviewConfig(enabled=enabled, daily_time="03:00"))


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
    await repo.save_review_report(day_start - 86400, day_start, "{}", "", "none")
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
    assert result == {"started": False, "error": "复盘进行中"}  # server 层映 409
    assert agent.calls == []


async def test_run_now_runs_yesterday_period(repo):
    agent = StubAgent()
    scheduler = ReviewScheduler(_settings(), agent, repo)
    day_start = local_day_start(time.time())
    result = await scheduler.run_now()
    assert result["started"] is True and result["ok"] is True
    assert agent.calls == [(day_start - 86400, day_start)]
