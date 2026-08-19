"""同步网关 I/O 统一异步卸载层（单线程执行 + 优先级调度）。

Gate SDK 为同步实现且不做线程安全假设：所有网关 I/O 必须经本层提交到唯一
单线程 executor 串行执行，协程侧仅 await 结果，不阻塞事件循环（issue #72）。
手动平仓/撤单等安全操作以 PRIORITY_HIGH 提交，插队于只读查询之前，避免被
慢查询排队饿死。运行约束：网关客户端只允许本层的单线程访问，禁止在 async
路径中直接调用同步网关方法（由架构守护测试兜底）。

PaperGateway 等纯内存实现无网络 I/O，且其撮合（on_price）、资金费结算与
drain_fills 均在事件循环线程直接修改同一账户状态：若账户类方法再进 executor
线程，单线程状态机就退化为跨线程共享可变状态（PR #84 评审）。因此网关类可
声明 __gateway_io_inline__ 标记纯内存方法名（含以网关为首参、内部仅调纯内存
方法的事务辅助函数名）；命中标记的调用不进 executor，直接在事件循环线程内联
执行，保持单线程语义。标记支持两种形态：frozenset[str] 静态方法名集合，或
Callable[[str], bool] 实例级判定函数（按实例状态动态判定，如 paper 的
get_tickers 仅在无真实 REST provider 时内联）；静态集合中不得加入转发真实
REST provider 的行情委托类方法（get_candlesticks/fetch_open_interest 等）。
"""

from __future__ import annotations

import asyncio
import functools
import itertools
import weakref
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, TypeVar

from .base import Gateway, GatewayError, Position

T = TypeVar("T")

# 优先级：数值小者先执行；手动安全操作（平仓/撤单）用 HIGH，只读查询与普通下单用 NORMAL
PRIORITY_HIGH = 0
PRIORITY_NORMAL = 1

_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gateway-io")
# 弱引用登记：测试等场景频繁创建/销毁事件循环，避免调度器实例滞留内存
_SCHEDULERS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _IoScheduler] = (
    weakref.WeakKeyDictionary()
)

# 孤儿写兜底：已 dispatch 到交易所的写请求因调用方取消/超时导致结果无人接收时
# 触发（参数为写操作名）。由应用组装层（AgentLoop）注册为"记审计日志 + 触发
# 风控锁"的 fail-closed 处理；未注册时静默（测试环境默认）。
_orphan_write_handler: Callable[[str], None] | None = None


def set_orphan_write_handler(handler: Callable[[str], None] | None) -> None:
    """注册"已下发写请求被取消"的兜底回调（应用组装期调用一次）。

    参数：
        handler: Callable[[str], None] | None，孤儿写回调（参数为写操作名）；
            None 表示注销

    返回：
        None，就地更新模块级回调
    """
    global _orphan_write_handler
    _orphan_write_handler = handler


class _DispatchProbe:
    """dispatch 探针：调度器把任务提交 executor 后记录返回的 concurrent Future。

    取消侧据此无窗口地区分三种状态：cf 为 None（任务仍在优先级队列，随调用方
    Future 一并撤回）；cf.cancel() 为 True（已提交但 worker 未开始执行，撤回
    成功）；否则（worker 已执行或已完成，结果将无人接收）——mutation 写此时
    必须按孤儿写 fail closed（PR #84 评审 P1：started 由 worker 线程置位，
    "已提交 executor、尚未开始执行"的窗口内取消会漏掉兜底）。
    """

    __slots__ = ("cf",)

    def __init__(self) -> None:
        """初始化空探针（任务尚未 dispatch）。

        参数：无
        返回：
            None，初始化实例字段（cf 由调度器 dispatch 时写入）
        """
        self.cf: Future | None = None


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
    ) -> tuple[asyncio.Future, _DispatchProbe]:
        """把一次同步网关调用排入优先级队列，返回可 await 的结果 Future 与 dispatch 探针。

        参数：
            fn: Callable，待执行的同步网关方法（绑定方法引用）
            args: tuple，位置参数
            kwargs: dict，关键字参数
            priority: int，优先级（PRIORITY_HIGH 插队于 PRIORITY_NORMAL 之前）
        返回：
            tuple[asyncio.Future, _DispatchProbe]：调用结果的 Future，以及
            dispatch 探针（调度器提交 executor 后写入 concurrent Future）；
            取消语义据此区分"仍在队列/已提交但撤回成功"与"已执行结果无人接收"；
            队列空转时自动启动消费协程
        """
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        probe = _DispatchProbe()
        self._queue.put_nowait((priority, next(self._seq), fn, args, kwargs, fut, probe))
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._drain())
        return fut, probe

    async def _drain(self) -> None:
        """消费协程：按优先级逐个取出任务，提交单线程 executor 执行并回写结果。

        参数：无
        返回：
            None，队列排空后退出（已取消的 Future 对应任务直接跳过）；退出前清空
            _worker 句柄——Task 强引用其 loop，持有已完成 Task 会让 WeakKeyDictionary
            的弱键失效、已销毁事件循环无法回收（PR #84 评审 P2）
        """
        try:
            while not self._queue.empty():
                # 每取一个任务前让出一次循环：刚完成任务的调用方协程得以恢复并把后续
                # 高优调用入队——否则消费协程 set_result 后同步续跑、在调用方提交下
                # 一个 HIGH 调用前就先取走已排队 NORMAL，优先级在串行调用链上失效
                # （PR #84 评审 P1：manual close 的风控读取被普通 backlog 穿插）
                await asyncio.sleep(0)
                _, _, fn, args, kwargs, fut, probe = await self._queue.get()
                if fut.cancelled():
                    continue
                # 直接持有 concurrent Future：取消侧可用 cf.cancel() 无窗口地区分
                # "已提交但撤回成功"与"已执行结果无人接收"（PR #84 评审 P1）
                cf = _EXECUTOR.submit(functools.partial(fn, *args, **kwargs))
                probe.cf = cf
                try:
                    result = await asyncio.wrap_future(cf)
                except Exception as e:  # 网关异常原样透传给调用方
                    if not fut.done():
                        fut.set_exception(e)
                else:
                    if not fut.done():
                        fut.set_result(result)
        finally:
            # 与 submit 同在事件循环线程、且循环退出到此处无 await 点，无竞态：
            # 清空后新提交会看到 _worker is None 并启动新的消费协程继续排空队列
            if self._worker is asyncio.current_task():
                self._worker = None


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


def _is_inline_call(fn: Callable[..., Any], args: tuple) -> bool:
    """判断本次调用是否命中网关的纯内存内联标记，应在事件循环线程直接执行。

    绑定方法取其 __self__，普通同步辅助取首参（约定为网关实例）；从实例读取
    __gateway_io_inline__ 标记（实例属性优先于类属性，支持按实例状态动态判定）：
    标记为 frozenset 时按函数名成员判定，为 Callable[[str], bool] 时调用其判定。
    内联执行保持 paper 账户的单线程状态机语义，避免与事件循环线程上的
    撮合/资金费/drain 并发改同一状态。

    参数：
        fn: Callable，待执行的同步函数（绑定方法或以网关为首参的辅助函数）
        args: tuple，位置参数
    返回：
        bool，命中内联标记返回 True，否则 False（应提交 executor 执行）
    """
    owner = getattr(fn, "__self__", None)
    if owner is None and args:
        owner = args[0]
    if owner is None:
        return False
    marker = getattr(owner, "__gateway_io_inline__", None)
    if callable(marker):
        return bool(marker(getattr(fn, "__name__", "")))
    return bool(marker) and getattr(fn, "__name__", "") in marker


async def run_gateway_io(
    fn: Callable[..., T],
    /,
    *args: Any,
    priority: int = PRIORITY_NORMAL,
    timeout: float | None = None,
    mutation: bool = False,
    **kwargs: Any,
) -> T:
    """经统一卸载层执行一次同步网关调用：优先级排队、单线程执行、协程侧 await。

    命中网关 __gateway_io_inline__ 标记的纯内存调用（如 PaperGateway 账户方法）
    不进 executor，直接在事件循环线程内联执行并返回；此时 priority/timeout 无意义
    （无 I/O 可等）。

    mutation=True（交易写路径）时的取消语义：调用方被取消/超时且写请求**已进入
    executor 执行**时，无法撤回线程内已发出的交易所请求——执行结果将无人接收
    （消费协程对已取消 Future 直接丢弃结果），此时触发模块级孤儿写回调
    （set_orphan_write_handler 注册），由应用层按"状态未知"落审计并触发风控锁。
    判定经 dispatch 探针无窗口完成：任务仍在优先级队列、或已提交 executor 但
    cf.cancel() 撤回成功（worker 未开始执行），均为安全撤回不触发；只有
    cf.cancel() 失败（已执行/已完成）才触发（PR #84 评审 P1：已下发写请求的
    结果不得静默丢失，且"已提交未开始"窗口不得漏判）。

    参数：
        fn: Callable[..., T]，同步网关方法（绑定方法引用，如 deps.gateway.list_positions）
        args: Any，位置参数
        priority: int，优先级；手动平仓/撤单等安全操作传 PRIORITY_HIGH
        timeout: float | None，整次调用的等待超时秒数；超时仅放弃等待并取消排队任务
            （线程内已开始的请求自行跑完，结果丢弃），None 表示不限时
        mutation: bool，是否为交易所写操作（下单/改单/撤单/调杠杆/保护单写）；
            写操作在 dispatch 后被取消且无法撤回时触发孤儿写兜底
        kwargs: Any，关键字参数
    返回：
        T，网关调用的返回值；网关异常原样向上抛
    异常：
        asyncio.TimeoutError：超过 timeout 仍未完成时抛出
        asyncio.CancelledError：调用方协程被取消时抛出（排队任务一并取消）
    """
    if _is_inline_call(fn, args):
        return fn(*args, **kwargs)  # 纯内存实现：事件循环线程内联，保持单线程状态机语义
    fut, probe = _scheduler().submit(fn, args, kwargs, priority)
    try:
        if timeout is None:
            return await fut
        return await asyncio.wait_for(asyncio.shield(fut), timeout)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        cf = probe.cf
        withdrawn = cf.cancel() if cf is not None else False
        fut.cancel()  # 仍在排队则取消；已执行则结果由消费协程丢弃
        if mutation and cf is not None and not withdrawn and _orphan_write_handler is not None:
            # 已执行/已完成且结果无人接收的写请求：交易所可能已生效——按状态未知
            # fail-closed（审计 + 风控锁），不得静默继续。cf.cancel() 成功代表
            # worker 未开始执行，请求从未到达交易所，安全撤回
            _orphan_write_handler(getattr(fn, "__name__", repr(fn)))
        raise


async def read_positions_with_tpsl(
    gateway: Gateway, *, priority: int = PRIORITY_NORMAL
) -> list[Position]:
    """展示用持仓读取：裸持仓 + 逐合约 TPSL 触发价补全（独立调度、单合约失败降级）。

    安全路径（人工平仓/风控/杠杆核验）禁止使用本组合读取：它们必须只用单次 REST
    的裸 list_positions。本函数把每个保护单子请求重新经过调度器——HIGH 安全操作
    可在子请求之间插队，且单合约查询失败仅使该合约止损/止盈降级为 None（展示为
    "未设置"），不拖垮整体读取（PR #84 评审 P1：N+1 复合读取阻断人工平仓）。

    参数：
        gateway: Gateway，交易网关
        priority: int，卸载优先级（默认 NORMAL）

    返回：
        list[Position]：持仓列表；补全成功的合约回填 stop_loss_price/take_profit_price，
        无对应保护单或查询失败的为 None
    """
    positions = await run_gateway_io(gateway.list_positions, priority=priority)
    for pos in positions:
        try:
            tpsl = await run_gateway_io(gateway.list_tpsl_orders, pos.contract, priority=priority)
        except GatewayError:
            continue  # 单合约保护单查询失败：该合约降级为"未设置"，不拖垮整体
        mine = [o for o in tpsl if o.direction == (1 if pos.size > 0 else -1)]
        pos.stop_loss_price = next((o.trigger_price for o in mine if o.kind == "stop_loss"), None)
        pos.take_profit_price = next(
            (o.trigger_price for o in mine if o.kind == "take_profit"), None
        )
    return positions
