"""统一卸载层（src.gateway.async_io）单元测试：

结果/异常透传、单线程串行、同优先级 FIFO、高优先级插队、
超时仅放弃等待不杀线程、排队任务可取消、慢同步调用不阻塞事件循环。"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from src.gateway.async_io import PRIORITY_HIGH, run_gateway_io


async def test_run_gateway_io_returns_result():
    """验证同步函数的返回值经卸载层原样透传给协程调用方。

    参数：无

    返回：
        None，断言返回值与位置/关键字参数传递正确
    """

    def _add(a: int, b: int, extra: int = 0) -> int:
        return a + b + extra

    assert await run_gateway_io(_add, 1, 2, extra=3) == 6


async def test_run_gateway_io_propagates_exception():
    """验证同步函数抛出的异常经卸载层原样透传，不包装不吞没。

    参数：无

    返回：
        None，断言抛出的异常类型与消息一致
    """

    def _boom():
        raise ValueError("gate exploded")

    with pytest.raises(ValueError, match="gate exploded"):
        await run_gateway_io(_boom)


async def test_run_gateway_io_executes_on_single_thread_serially():
    """验证所有任务在同一网关线程串行执行（SDK 客户端非线程安全约束）。

    参数：无

    返回：
        None，断言全部任务线程 ident 相同且无并发重叠
    """
    idents: list[int] = []
    active = False
    overlap = False

    def _probe():
        nonlocal active, overlap
        if active:
            overlap = True
        active = True
        idents.append(threading.get_ident())
        time.sleep(0.01)
        active = False
        return True

    await asyncio.gather(*(run_gateway_io(_probe) for _ in range(8)))
    assert len(set(idents)) == 1
    assert overlap is False


async def test_run_gateway_io_fifo_within_same_priority():
    """验证同优先级任务严格按提交顺序 FIFO 执行。

    参数：无

    返回：
        None，断言完成顺序与提交顺序一致
    """
    started = threading.Event()
    release = threading.Event()
    order: list[str] = []

    def _blocker():
        started.set()
        release.wait(timeout=5)

    def _record(name: str):
        order.append(name)

    first = asyncio.ensure_future(run_gateway_io(_blocker))
    await asyncio.to_thread(started.wait, 5)  # 等阻塞任务先占住网关线程
    tasks = [asyncio.ensure_future(run_gateway_io(_record, f"n{i}")) for i in range(5)]
    release.set()
    await first
    await asyncio.gather(*tasks)
    assert order == [f"n{i}" for i in range(5)]


async def test_run_gateway_io_high_priority_jumps_queue():
    """验证高优先级任务插队于已排队普通任务之前（手动安全操作不被只读查询饿死）。

    参数：无

    返回：
        None，断言 HIGH 任务先于先提交的 NORMAL 任务执行
    """
    started = threading.Event()
    release = threading.Event()
    order: list[str] = []

    def _blocker():
        started.set()
        release.wait(timeout=5)

    def _record(name: str):
        order.append(name)

    first = asyncio.ensure_future(run_gateway_io(_blocker))
    await asyncio.to_thread(started.wait, 5)
    normal = asyncio.ensure_future(run_gateway_io(_record, "normal"))
    high = asyncio.ensure_future(run_gateway_io(_record, "high", priority=PRIORITY_HIGH))
    release.set()
    await asyncio.gather(first, normal, high)
    assert order == ["high", "normal"]


async def test_run_gateway_io_timeout_abandons_wait_but_thread_survives():
    """验证超时仅解除调用方等待：抛出 TimeoutError，线程内请求自行跑完，后续调用不受影响。

    参数：无

    返回：
        None，断言超时抛出、慢任务线程内完结、卸载层随后仍可用
    """
    done = threading.Event()

    def _slow():
        time.sleep(0.3)
        done.set()
        return "late"

    with pytest.raises(asyncio.TimeoutError):
        await run_gateway_io(_slow, timeout=0.05)
    assert await run_gateway_io(lambda: "ok") == "ok"
    await asyncio.to_thread(done.wait, 5)  # 线程内慢请求最终自行完结


async def test_run_gateway_io_cancel_queued_task_never_executes():
    """验证仍在排队的任务被取消后不会进入网关线程执行。

    参数：无

    返回：
        None，断言被取消的排队任务其同步函数从未运行
    """
    started = threading.Event()
    release = threading.Event()
    executed = False

    def _blocker():
        started.set()
        release.wait(timeout=5)

    def _should_not_run():
        nonlocal executed
        executed = True

    first = asyncio.ensure_future(run_gateway_io(_blocker))
    await asyncio.to_thread(started.wait, 5)
    cancelled = asyncio.ensure_future(run_gateway_io(_should_not_run))
    await asyncio.sleep(0)  # 让取消目标任务完成入队
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    release.set()
    await first
    await asyncio.sleep(0.05)  # 给消费协程跳过取消任务的时间
    assert executed is False


async def test_run_gateway_io_slow_call_does_not_block_event_loop():
    """验证慢同步网关调用经卸载后不冻结事件循环，心跳协程持续推进。

    参数：无

    返回：
        None，断言 0.3s 慢调用期间 0.05s 心跳至少推进 3 次
    """

    def _slow():
        time.sleep(0.3)
        return "done"

    ticks = 0
    stop = False

    async def _ticker():
        """每 0.05s 累加一次 tick 的心跳协程，用于探测事件循环是否被阻塞。

        参数：无

        返回：
            None，stop 置位后退出循环
        """
        nonlocal ticks, stop
        while not stop:
            ticks += 1
            await asyncio.sleep(0.05)

    ticker = asyncio.ensure_future(_ticker())
    try:
        assert await run_gateway_io(_slow) == "done"
    finally:
        stop = True
        await ticker
    assert ticks >= 3


class _InlineMarkedGateway:
    """声明纯内存内联标记的伪网关：ping 命中标记，fetch 未命中。"""

    __gateway_io_inline__ = frozenset({"ping"})

    def ping(self) -> int:
        """返回当前执行线程 ident（用于断言执行线程亲和性）。

        参数：无
        返回：
            int：执行该方法的操作系统线程 ident
        """
        return threading.get_ident()

    def fetch(self) -> int:
        """返回当前执行线程 ident（未在内联标记集合中，应被卸载到 executor）。

        参数：无
        返回：
            int：执行该方法的操作系统线程 ident
        """
        return threading.get_ident()


async def test_inline_marked_call_skips_executor():
    """验证命中 __gateway_io_inline__ 标记的调用在事件循环线程内联执行、不经 executor 排队。

    参数：无

    返回：
        None，断言 executor 被占住时内联调用立即完成且运行在事件循环线程
    """
    started = threading.Event()
    release = threading.Event()

    def _blocker():
        started.set()
        release.wait(timeout=5)

    blocker = asyncio.ensure_future(run_gateway_io(_blocker))
    await asyncio.to_thread(started.wait, 5)  # 先占住唯一网关线程
    try:
        gateway = _InlineMarkedGateway()
        # executor 被占住期间，内联调用不排队、立即完成
        ident = await asyncio.wait_for(run_gateway_io(gateway.ping), timeout=1)
        assert ident == threading.get_ident()  # 事件循环线程（测试主线程）
    finally:
        release.set()
        await blocker


async def test_unmarked_gateway_method_still_offloaded():
    """验证未命中内联标记的同名网关方法与未登记辅助函数仍卸载到 executor 线程。

    参数：无

    返回：
        None，断言未标记方法与未登记辅助的执行线程不是事件循环线程
    """
    loop_ident = threading.get_ident()
    gateway = _InlineMarkedGateway()
    # fetch 不在标记集合：卸载
    assert await run_gateway_io(gateway.fetch) != loop_ident

    def _unregistered_helper(gw: _InlineMarkedGateway) -> int:
        return threading.get_ident()

    # 以网关为首参但函数名未登记：同样卸载（防止任意辅助绕过 executor）
    assert await run_gateway_io(_unregistered_helper, gateway) != loop_ident


def test_scheduler_entry_released_after_loop_closed():
    """验证事件循环销毁后调度器条目可被回收（_worker 句柄不再回链引用 loop）。

    参数：无

    返回：
        None，断言新事件循环关闭并 gc 后弱引用失效（修复前因 _worker 持有已完成
        Task 强引用 loop，弱键永不失效）
    """
    import gc
    import weakref

    from src.gateway import async_io

    loop = asyncio.new_event_loop()

    async def _once() -> int:
        return await async_io.run_gateway_io(lambda: 1)

    assert loop.run_until_complete(_once()) == 1
    ref = weakref.ref(loop)
    loop.close()
    del loop
    gc.collect()
    assert ref() is None


class _DynamicMarkedGateway:
    """实例级 callable 内联标记的伪网关：开关开时 ping 内联，关闭后卸载。"""

    def __init__(self) -> None:
        """初始化内联开关与实例级 callable 标记。

        参数：无
        返回：
            None，初始化实例字段（副作用：登记实例级 __gateway_io_inline__ 判定函数）
        """
        self.inline_enabled = True
        self.__gateway_io_inline__ = lambda name: self.inline_enabled and name == "ping"

    def ping(self) -> int:
        """返回当前执行线程 ident（用于断言执行线程亲和性）。

        参数：无
        返回：
            int：执行该方法的操作系统线程 ident
        """
        return threading.get_ident()


async def test_callable_marker_supports_dynamic_inline_judgement():
    """验证实例级 callable 标记按实例状态动态判定内联/卸载（paper get_tickers 同款机制）。

    参数：无

    返回：
        None，断言开关开时 ping 在事件循环线程内联执行、关闭后卸载到 executor 线程
    """
    gateway = _DynamicMarkedGateway()
    loop_ident = threading.get_ident()
    assert await run_gateway_io(gateway.ping) == loop_ident
    gateway.inline_enabled = False
    assert await run_gateway_io(gateway.ping) != loop_ident
