"""WakeupScheduler 单元测试：唤醒钳制、定时唤醒、抢醒、防重入、stop 清理。

时间相关逻辑通过注入 FakeSleep 控制（sleep_fn 依赖注入），避免真实等待分钟级时间。
"""

import asyncio

import pytest

from src.config import SchedulerConfig
from src.scheduler import WakeupScheduler


class FakeSleep:
    """可控假睡眠：记录请求的秒数；测试放行（release.set）后定时器才真正到期。"""

    def __init__(self) -> None:
        """初始化测试替身及其可观测状态。

        参数：
            self: FakeSleep，当前测试替身实例
        返回：
            None，初始化并保存测试替身状态
        """
        self.calls: list[float] = []
        self.release = asyncio.Event()

    async def __call__(self, seconds: float) -> None:
        """记录测试替身收到的调用参数。

        参数：
            self: FakeSleep，当前测试替身实例
            seconds: float，休眠或调度秒数
        返回：
            None，返回该测试辅助函数构造或记录的结果
        """
        self.calls.append(seconds)
        await self.release.wait()
        self.release.clear()


class WakeRecorder:
    """记录唤醒来源，并通过 event 通知测试有新唤醒到达。"""

    def __init__(self) -> None:
        """初始化测试替身及其可观测状态。

        参数：
            self: WakeRecorder，当前测试替身实例
        返回：
            None，初始化并保存测试替身状态
        """
        self.sources: list[str] = []
        self.event = asyncio.Event()

    async def __call__(self, source: str) -> None:
        """记录测试替身收到的调用参数。

        参数：
            self: WakeRecorder，当前测试替身实例
            source: str，唤醒或成交来源
        返回：
            None，返回该测试辅助函数构造或记录的结果
        """
        self.sources.append(source)
        self.event.set()


@pytest.fixture
def config() -> SchedulerConfig:
    """创建调度器测试配置。

    参数：无
    返回：
        SchedulerConfig，返回该测试辅助函数构造或记录的结果
    """
    return SchedulerConfig(default_wake_minutes=60, min_wake_minutes=5, max_wake_minutes=720)


async def _wait_until(pred, timeout: float = 1.0) -> None:
    """轮询等待谓词成立（让调度循环有机会跑完决策轮/重新武装定时器）。

    参数：
        pred: Callable[[], bool]，等待成立的条件函数
        timeout: float，最大等待秒数
    返回：
        None，返回该测试辅助函数构造或记录的结果
    """

    async def _poll() -> None:
        """轮询条件是否在超时时间内成立。

        参数：无
        返回：
            None，返回该测试辅助函数构造或记录的结果
        """
        while not pred():
            await asyncio.sleep(0)

    await asyncio.wait_for(_poll(), timeout)


# ---------- 唤醒间隔钳制 ----------


async def test_set_next_wake_clamps(config: SchedulerConfig):
    """验证下次唤醒间隔会被限制在允许范围内。

    参数：
        config: SchedulerConfig，调度器测试配置
    返回：
        None，执行断言验证目标行为
    """
    sched = WakeupScheduler(config, WakeRecorder(), sleep_fn=FakeSleep())
    assert sched.set_next_wake(1) == 5  # 低于下限，钳到 min_wake_minutes
    assert sched.set_next_wake(9999) == 720  # 高于上限，钳到 max_wake_minutes
    assert sched.set_next_wake(30) == 30  # 区间内保持不变


# ---------- 定时唤醒 ----------


async def test_timer_wake_uses_default_minutes(config: SchedulerConfig):
    """验证定时唤醒会使用配置中的默认间隔。

    参数：
        config: SchedulerConfig，调度器测试配置
    返回：
        None，执行断言验证目标行为
    """
    rec = WakeRecorder()
    fake = FakeSleep()
    sched = WakeupScheduler(config, rec, sleep_fn=fake)
    await sched.start()
    await _wait_until(lambda: len(fake.calls) == 1)  # 等定时器任务注册假睡眠
    assert fake.calls == [3600.0]  # 未设置时用 default_wake_minutes=60
    fake.release.set()  # 定时器到期
    await asyncio.wait_for(rec.event.wait(), 1)
    assert rec.sources == ["timer:60min"]
    await sched.stop()


async def test_end_round_rearms_with_llm_minutes(config: SchedulerConfig):
    """验证轮次结束后会按模型给出的分钟数重新设定计时器。

    参数：
        config: SchedulerConfig，调度器测试配置
    返回：
        None，执行断言验证目标行为
    """
    rec = WakeRecorder()
    fake = FakeSleep()

    async def on_wake(source: str) -> None:
        """记录调度器发出的唤醒来源。

        参数：
            source: str，唤醒或成交来源
        返回：
            None，返回该测试辅助函数构造或记录的结果
        """
        await rec(source)  # 记录唤醒并通知测试
        sched.set_next_wake(30)  # LLM 自设下次 30 分钟后唤醒

    sched = WakeupScheduler(config, on_wake, sleep_fn=fake)
    await sched.start()
    await _wait_until(lambda: len(fake.calls) == 1)  # 首次武装：默认 60min
    sched.wake_now("trigger:boot")
    await asyncio.wait_for(rec.event.wait(), 1)
    await _wait_until(lambda: len(fake.calls) == 2)
    assert fake.calls == [3600.0, 1800.0]  # 首次默认 60min，第二轮改用 LLM 设的 30min
    await sched.stop()


# ---------- 抢醒 ----------


async def test_wake_now_preempts_timer(config: SchedulerConfig):
    """验证立即唤醒会抢占尚未到期的定时器。

    参数：
        config: SchedulerConfig，调度器测试配置
    返回：
        None，执行断言验证目标行为
    """
    rec = WakeRecorder()
    fake = FakeSleep()
    sched = WakeupScheduler(config, rec, sleep_fn=fake)
    await sched.start()
    await _wait_until(lambda: len(fake.calls) == 1)  # 定时器已武装（假睡眠挂起中）
    assert sched.wake_now("price_trigger:BTC_USDT") is True
    await asyncio.wait_for(rec.event.wait(), 1)  # 定时器未到期（未放行），抢醒先行
    assert rec.sources == ["price_trigger:BTC_USDT"]
    await _wait_until(lambda: len(fake.calls) == 2)
    assert fake.calls == [3600.0, 3600.0]  # 抢醒的轮次结束后按默认值重新武装
    await sched.stop()


# ---------- 防重入 ----------


async def test_wake_now_deferred_during_round(config: SchedulerConfig):
    """决策轮内 wake_now 不立即生效（防重入），记 pending 并在轮末补一次唤醒。

    参数：
        config: SchedulerConfig，调度器测试配置
    返回：
        None，执行断言验证目标行为
    """
    entered = asyncio.Event()
    release_round = asyncio.Event()
    rec = WakeRecorder()

    async def on_wake(source: str) -> None:
        """记录调度器发出的唤醒来源。

        参数：
            source: str，唤醒或成交来源
        返回：
            None，返回该测试辅助函数构造或记录的结果
        """
        rec.sources.append(source)
        entered.set()
        await release_round.wait()  # 决策轮保持进行中，直到测试放行

    sched = WakeupScheduler(config, on_wake, sleep_fn=FakeSleep())
    await sched.start()
    assert sched.wake_now("trigger:first") is True
    await asyncio.wait_for(entered.wait(), 1)
    assert sched.in_round is True
    assert sched.wake_now("trigger:second") is True  # 轮内抢醒进入 pending
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert rec.sources == ["trigger:first"]  # 轮内不立即唤醒
    release_round.set()
    await _wait_until(lambda: len(rec.sources) == 2)  # 轮末补一次唤醒
    assert rec.sources == ["trigger:first", "trigger:second"]
    await sched.stop()


async def test_pending_wakes_merged_into_one(config: SchedulerConfig):
    """轮内多次抢醒只保留最后一个原因，轮末合并补一次唤醒（不连补多轮）。

    参数：
        config: SchedulerConfig，调度器测试配置
    返回：
        None，执行断言验证目标行为
    """
    entered = asyncio.Event()
    release_round = asyncio.Event()
    rec = WakeRecorder()

    async def on_wake(source: str) -> None:
        """记录调度器发出的唤醒来源。

        参数：
            source: str，唤醒或成交来源
        返回：
            None，返回该测试辅助函数构造或记录的结果
        """
        rec.sources.append(source)
        entered.set()
        await release_round.wait()

    sched = WakeupScheduler(config, on_wake, sleep_fn=FakeSleep())
    await sched.start()
    sched.wake_now("trigger:first")
    await asyncio.wait_for(entered.wait(), 1)
    sched.wake_now("trigger:second")
    sched.wake_now("trigger:third")
    release_round.set()
    await _wait_until(lambda: len(rec.sources) == 2)  # 轮末补一次唤醒
    await sched.stop()  # 若多补了轮次会体现在 sources 里
    assert rec.sources == ["trigger:first", "trigger:third"]  # 合并为一次，取最后原因


async def test_pending_wake_dropped_when_stopped_during_round(config: SchedulerConfig):
    """决策轮内 stop：调度器停止后丢弃 pending 抢醒。

    参数：
        config: SchedulerConfig，调度器测试配置
    返回：
        None，执行断言验证目标行为
    """
    entered = asyncio.Event()
    release_round = asyncio.Event()
    rec = WakeRecorder()

    async def on_wake(source: str) -> None:
        """记录调度器发出的唤醒来源。

        参数：
            source: str，唤醒或成交来源
        返回：
            None，返回该测试辅助函数构造或记录的结果
        """
        rec.sources.append(source)
        entered.set()
        await release_round.wait()

    sched = WakeupScheduler(config, on_wake, sleep_fn=FakeSleep())
    await sched.start()
    sched.wake_now("trigger:first")
    await asyncio.wait_for(entered.wait(), 1)
    assert sched.wake_now("trigger:second") is True  # 记为 pending
    stop_task = asyncio.create_task(sched.stop())
    await asyncio.sleep(0)  # 让 stop 先把 _running 置 False
    release_round.set()
    await asyncio.wait_for(stop_task, 1)
    assert rec.sources == ["trigger:first"]  # pending 因 stop 被丢弃
    assert sched.is_running is False


async def test_timer_expiry_during_round_dropped(config: SchedulerConfig):
    """验证轮次执行期间到期的定时唤醒会被丢弃。

    参数：
        config: SchedulerConfig，调度器测试配置
    返回：
        None，执行断言验证目标行为
    """
    entered = asyncio.Event()
    release_round = asyncio.Event()
    rec = WakeRecorder()
    fake = FakeSleep()

    async def on_wake(source: str) -> None:
        """记录调度器发出的唤醒来源。

        参数：
            source: str，唤醒或成交来源
        返回：
            None，返回该测试辅助函数构造或记录的结果
        """
        rec.sources.append(source)
        entered.set()
        await release_round.wait()

    sched = WakeupScheduler(config, on_wake, sleep_fn=fake)
    await sched.start()  # 武装 60min 定时器（假睡眠挂起中）
    sched.wake_now("trigger:boot")  # 抢醒进入决策轮，定时器仍在挂起
    await asyncio.wait_for(entered.wait(), 1)
    fake.release.set()  # 定时器在决策轮内到期 → 应被丢弃
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert rec.sources == ["trigger:boot"]
    release_round.set()
    await sched.stop()


# ---------- 生命周期 ----------


async def test_wake_now_before_start_rejected(config: SchedulerConfig):
    """验证调度器启动前的立即唤醒请求会被拒绝。

    参数：
        config: SchedulerConfig，调度器测试配置
    返回：
        None，执行断言验证目标行为
    """
    sched = WakeupScheduler(config, WakeRecorder(), sleep_fn=FakeSleep())
    assert sched.wake_now("trigger:early") is False


async def test_stop_cancels_timer_and_ignores_wake(config: SchedulerConfig):
    """验证停止调度器会取消计时器并忽略后续唤醒。

    参数：
        config: SchedulerConfig，调度器测试配置
    返回：
        None，执行断言验证目标行为
    """
    rec = WakeRecorder()
    fake = FakeSleep()
    sched = WakeupScheduler(config, rec, sleep_fn=fake)
    await sched.start()
    await sched.stop()
    assert sched.is_running is False
    assert sched.wake_now("trigger:after_stop") is False  # 停止后抢醒无效
    fake.release.set()  # 已取消的定时器即使放行也不会再触发
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert rec.sources == []
    assert sched._loop_task is None and sched._timer_task is None  # 任务已清理
