"""同步网关 I/O 统一异步卸载层（单线程执行 + 优先级调度）。

Gate SDK 为同步实现且不做线程安全假设：所有网关 I/O 必须经本层提交到唯一
单线程 executor 串行执行，协程侧仅 await 结果，不阻塞事件循环（issue #72）。
手动平仓/撤单等安全操作以 PRIORITY_HIGH 提交，插队于只读查询之前，避免被
慢查询排队饿死。运行约束：网关客户端只允许本层的单线程访问，禁止在 async
路径中直接调用同步网关方法（由架构守护测试兜底）。

PaperGateway 等纯内存实现无网络 I/O，按 issue #72 决议不强制经本层。
"""

from __future__ import annotations

import asyncio
import functools
import itertools
import weakref
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypeVar

T = TypeVar("T")

# 优先级：数值小者先执行；手动安全操作（平仓/撤单）用 HIGH，只读查询与普通下单用 NORMAL
PRIORITY_HIGH = 0
PRIORITY_NORMAL = 1

_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gateway-io")
# 弱引用登记：测试等场景频繁创建/销毁事件循环，避免调度器实例滞留内存
_SCHEDULERS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _IoScheduler] = (
    weakref.WeakKeyDictionary()
)


class _IoScheduler:
    """per-loop 优先级调度器：队列驱动消费协程，单线程串行执行同步网关调用。

    消费协程按需启动、队列排空即退出，不产生常驻任务；同优先级按提交顺序 FIFO。
    """

    def __init__(self) -> None:
        """初始化空优先级队列与序列号发生器。

        参数：无
        返回：
            None，初始化实例字段（副作用：创建空队列与任务句柄）
        """
        self._queue: asyncio.PriorityQueue[tuple] = asyncio.PriorityQueue()
        self._seq = itertools.count()
        self._worker: asyncio.Task | None = None

    def submit(
        self, fn: Callable[..., Any], args: tuple, kwargs: dict, priority: int
    ) -> asyncio.Future:
        """把一次同步网关调用排入优先级队列，返回可 await 的结果 Future。

        参数：
            fn: Callable，待执行的同步网关方法（绑定方法引用）
            args: tuple，位置参数
            kwargs: dict，关键字参数
            priority: int，优先级（PRIORITY_HIGH 插队于 PRIORITY_NORMAL 之前）
        返回：
            asyncio.Future，调用结果的 Future；队列空转时自动启动消费协程
        """
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._queue.put_nowait((priority, next(self._seq), fn, args, kwargs, fut))
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._drain())
        return fut

    async def _drain(self) -> None:
        """消费协程：按优先级逐个取出任务，提交单线程 executor 执行并回写结果。

        参数：无
        返回：
            None，队列排空后退出（已取消的 Future 对应任务直接跳过）
        """
        loop = asyncio.get_running_loop()
        while not self._queue.empty():
            _, _, fn, args, kwargs, fut = await self._queue.get()
            if fut.cancelled():
                continue
            try:
                result = await loop.run_in_executor(
                    _EXECUTOR, functools.partial(fn, *args, **kwargs)
                )
            except Exception as e:  # 网关异常原样透传给调用方
                if not fut.done():
                    fut.set_exception(e)
            else:
                if not fut.done():
                    fut.set_result(result)


def _scheduler() -> _IoScheduler:
    """取当前事件循环的调度器（不存在则创建并登记）。

    参数：无
    返回：
        _IoScheduler，当前运行中事件循环专属的调度器实例
    """
    loop = asyncio.get_running_loop()
    scheduler = _SCHEDULERS.get(loop)
    if scheduler is None:
        scheduler = _IoScheduler()
        _SCHEDULERS[loop] = scheduler
    return scheduler


async def run_gateway_io(
    fn: Callable[..., T],
    /,
    *args: Any,
    priority: int = PRIORITY_NORMAL,
    timeout: float | None = None,
    **kwargs: Any,
) -> T:
    """经统一卸载层执行一次同步网关调用：优先级排队、单线程执行、协程侧 await。

    参数：
        fn: Callable[..., T]，同步网关方法（绑定方法引用，如 deps.gateway.list_positions）
        args: Any，位置参数
        priority: int，优先级；手动平仓/撤单等安全操作传 PRIORITY_HIGH
        timeout: float | None，整次调用的等待超时秒数；超时仅放弃等待并取消排队任务
            （线程内已开始的请求自行跑完，结果丢弃），None 表示不限时
        kwargs: Any，关键字参数
    返回：
        T，网关调用的返回值；网关异常原样向上抛
    异常：
        asyncio.TimeoutError：超过 timeout 仍未完成时抛出
        asyncio.CancelledError：调用方协程被取消时抛出（排队任务一并取消）
    """
    fut = _scheduler().submit(fn, args, kwargs, priority)
    try:
        if timeout is None:
            return await fut
        return await asyncio.wait_for(asyncio.shield(fut), timeout)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        fut.cancel()  # 仍在排队则取消；已执行则结果由消费协程丢弃
        raise
