"""issue #72 回归：各异步路径注入慢同步网关实现后，事件循环心跳仍持续推进。

覆盖 LLM 下单全链路、手动平仓、决策上下文构建、open orders 分页端点辅助；
另覆盖两处分页查询的总页数上限（PAGINATION_OVERFLOW，防分页异常死循环）。
PR #84 评审追加：PaperGateway 账户方法线程亲和（内联不进 executor）、手动平仓
全程高优先级插队、慢分页期间 HIGH 任务页间插入、并发 update_tpsl 原子性
（不留两套新保护）、manual close 不插入 TPSL 交换中段。
PR #84 第三轮评审追加：持仓 TPSL 补全与风控快照元数据逐合约独立调度
（HIGH 安全操作可在合约间隙插队）、TPSL 补全单合约失败降级、安全路径零
TPSL 依赖（保护单接口故障时人工平仓照常）。
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from src.agent.context import ContextBuilder, position_snapshots
from src.agent import tool_tpsl
from src.agent.manual_close import close_position
from src.agent.tool_handlers import ToolOutcome
from src.agent.tool_leverage import (
    _amend_unless_close_intervened,
    _close_and_bump_epoch,
    _place_unless_close_intervened,
    _place_with_rollback,
)
from src.agent.tool_amend import _amend_direction
from src.agent.tool_tpsl import update_tpsl
from src.gateway.async_io import (
    _EXECUTOR,
    PRIORITY_HIGH,
    PRIORITY_NORMAL,
    _is_inline_call,
    _scheduler,
    read_positions_with_tpsl,
    run_gateway_io,
)
from src.gateway.base import GatewayError, OrderRequest, OrderStateUnknown, Position, TpslOrder
from src.market.candles import CandleCache, ManualPriceSource
from src.market.triggers import TriggerManager
from src.server.routes_status import _account_equity
from src.server.routes_trading import _list_all_open_orders
from tests.test_agent_tools_risk import _long_position, _make_tools, _open_limit_order
from tests.test_paper import buy, make_gateway


def _slow(fn: Callable, delay: float = 0.3) -> Callable:
    """把同步网关方法包装为延迟 delay 秒的慢实现（模拟 Gate REST 慢响应）。

    参数：
        fn: Callable，原始同步方法
        delay: float，注入的同步阻塞秒数；省略时默认 0.3

    返回：
        Callable：先 time.sleep(delay) 再转发原实现的包装函数
    """

    def _wrapped(*args: Any, **kwargs: Any) -> Any:
        time.sleep(delay)
        return fn(*args, **kwargs)

    return _wrapped


async def _run_with_heartbeat(coro: Any, interval: float = 0.05) -> tuple[Any, int]:
    """并发运行目标协程与心跳协程，返回（目标结果, 心跳次数）。

    参数：
        coro: Any，待运行的目标协程
        interval: float，心跳间隔秒数；省略时默认 0.05

    返回：
        tuple[Any, int]：目标协程返回值与期间心跳推进次数
    """
    ticks = 0
    stop = False

    async def _ticker() -> None:
        """心跳协程：每 interval 累加一次 tick 直到 stop 置位。

        参数：无

        返回：
            None，stop 置位后退出循环
        """
        nonlocal ticks, stop
        while not stop:
            ticks += 1
            await asyncio.sleep(interval)

    ticker = asyncio.ensure_future(_ticker())
    try:
        result = await coro
    finally:
        stop = True
        await ticker
    return result, ticks


async def test_place_order_slow_gateway_keeps_heartbeat(tmp_path):
    """验证 LLM 下单全链路在慢 list_positions 下事件循环心跳仍推进。

    参数：
        tmp_path: Path，pytest 临时目录夹具

    返回：
        None，断言下单放行且 0.3s 慢读取期间心跳至少推进 3 次
    """
    env = await _make_tools(tmp_path)
    try:
        env.gateway.list_positions = _slow(env.gateway.list_positions)
        out, ticks = await _run_with_heartbeat(
            env.registry.execute(
                "place_order",
                {
                    "contract": "BTC_USDT",
                    "side": "long",
                    "margin_usdt": 60,
                    "leverage": 1,
                    "stop_loss_price": 58000,
                },
            )
        )
        assert out.risk_verdict == "allow", out.text
        assert ticks >= 3
    finally:
        await env.db.close()


async def test_manual_close_slow_gateway_keeps_heartbeat(tmp_path):
    """验证手动平仓在慢持仓/账户/合约读取下事件循环心跳仍推进。

    参数：
        tmp_path: Path，pytest 临时目录夹具

    返回：
        None，断言平仓放行且慢读取期间心跳至少推进 3 次
    """
    env = await _make_tools(tmp_path)
    try:
        env.gateway.positions["BTC_USDT"] = _long_position("2")
        env.gateway.list_positions = _slow(env.gateway.list_positions)
        env.gateway.get_account = _slow(env.gateway.get_account)
        env.gateway.get_contract = _slow(env.gateway.get_contract)
        result, ticks = await _run_with_heartbeat(close_position(env.deps, "BTC_USDT"))
        assert result.outcome.risk_verdict == "allow", result.outcome.text
        assert ticks >= 3
    finally:
        await env.db.close()


async def test_context_build_slow_gateway_keeps_heartbeat(tmp_path):
    """验证决策上下文构建在慢账户/ticker 读取下事件循环心跳仍推进。

    参数：
        tmp_path: Path，pytest 临时目录夹具

    返回：
        None，断言上下文成功组装且慢读取期间心跳至少推进 3 次
    """
    env = await _make_tools(tmp_path)
    try:
        env.gateway.get_account = _slow(env.gateway.get_account)
        env.gateway.get_tickers = _slow(env.gateway.get_tickers)
        builder = ContextBuilder(
            env.gateway,
            env.repo,
            CandleCache(env.gateway, ManualPriceSource()),
            TriggerManager(lambda t, p: None),
            ["BTC_USDT"],
        )
        ctx, ticks = await _run_with_heartbeat(builder.build("test"))
        assert "## 账户" in ctx.text
        assert ticks >= 3
    finally:
        await env.db.close()


class _StubOrdersGateway:
    """最小订单分页存根：按预设页返回，支持注入每页延迟。"""

    def __init__(self, pages: list[int], delay: float = 0.0) -> None:
        """初始化存根：pages 为每页返回的订单条数序列，delay 为每页延迟秒数。

        参数：
            pages: list[int]，每次分页调用依次返回的订单条数；超出序列后按最后
                一页条数继续返回（用于构造永不结束的分页）
            delay: float，每次调用前同步阻塞的秒数；省略时默认 0

        返回：
            None，就地初始化实例属性
        """
        self._pages = pages
        self._delay = delay

    def list_orders(self, *args: Any, **kwargs: Any) -> list[Any]:
        """按序列返回一页伪订单（SimpleNamespace 鸭子类型）。

        参数：
            args: tuple，位置参数（忽略）
            kwargs: dict，关键字参数（忽略）

        返回：
            list[Any]：本页伪订单列表
        """
        if self._delay:
            time.sleep(self._delay)
        count = self._pages[0] if len(self._pages) == 1 else self._pages.pop(0)
        return [SimpleNamespace(id=str(i)) for i in range(count)]


async def test_open_orders_pagination_slow_gateway_keeps_heartbeat():
    """验证 open orders 分页拉取经卸载层后，慢分页期间事件循环心跳仍推进。

    参数：无

    返回：
        None，断言两页拉全且每页 0.2s 慢响应期间心跳至少推进 3 次
    """
    stub = _StubOrdersGateway([100, 30], delay=0.2)
    orders, ticks = await _run_with_heartbeat(_list_all_open_orders(stub))
    assert len(orders) == 130
    assert ticks >= 3


async def test_open_orders_pagination_cap_raises():
    """验证分页永不结束时按总页数上限抛 PAGINATION_OVERFLOW，而非无限循环。

    参数：无

    返回：
        None，断言第 50 页后抛出 label=PAGINATION_OVERFLOW 的 GatewayError
    """
    stub = _StubOrdersGateway([100])
    with pytest.raises(GatewayError) as excinfo:
        await _list_all_open_orders(stub)
    assert excinfo.value.label == "PAGINATION_OVERFLOW"


async def test_manual_cancel_open_check_pagination_cap():
    """验证手动撤单前置核对的分页永不结束时同样按上限抛错。

    参数：无

    返回：
        None，断言 _require_open_order 抛出 label=PAGINATION_OVERFLOW 的 GatewayError
    """
    from src.agent.manual_cancel import _require_open_order

    deps = SimpleNamespace(gateway=_StubOrdersGateway([100]))
    with pytest.raises(GatewayError) as excinfo:
        await _require_open_order(deps, "BTC_USDT", "999")
    assert excinfo.value.label == "PAGINATION_OVERFLOW"


# ---------- PR #84 评审回归：paper 线程亲和 / 优先级端到端 / TPSL 原子性 ----------


async def test_paper_account_methods_run_inline_while_executor_busy():
    """验证 PaperGateway 账户方法命中内联标记：executor 被占住时仍立即在事件循环线程完成。

    参数：无

    返回：
        None，断言慢任务占住唯一网关线程期间 paper 查仓/下单不排队直接完成，
        且成交缓冲可正常 drain（单线程状态机语义未退化）
    """
    paper = make_gateway()
    started = threading.Event()
    release = threading.Event()

    def _blocker():
        started.set()
        release.wait(timeout=5)

    blocker = asyncio.ensure_future(run_gateway_io(_blocker))
    await asyncio.to_thread(started.wait, 5)  # 先占住唯一网关线程
    try:
        positions = await asyncio.wait_for(run_gateway_io(paper.list_positions), timeout=1)
        assert positions == []
        result = await asyncio.wait_for(
            run_gateway_io(
                paper.place_order, OrderRequest(contract="BTC_USDT", size=Decimal(1), price=None)
            ),
            timeout=1,
        )
        assert result.id
    finally:
        release.set()
        await blocker
    fills = paper.drain_fills()
    assert len(fills) == 1  # 市价单立即成交并进入缓冲（inline 未破坏撮合语义）


async def test_manual_close_all_gateway_calls_jump_normal_backlog(tmp_path):
    """验证手动平仓全程高优先级：普通 backlog 排队时其所有网关调用均先于 backlog 执行。

    参数：
        tmp_path: Path，pytest 临时目录夹具

    返回：
        None，断言平仓的 list_positions/get_contract/get_account/place_order 全部
        先于先提交的普通任务执行（风控读取不再掉回普通队尾）
    """
    env = await _make_tools(tmp_path)
    try:
        env.gateway.positions["BTC_USDT"] = _long_position("2")
        calls: list[str] = []
        for name in ("list_positions", "get_contract", "get_account", "place_order"):
            orig = getattr(env.gateway, name)

            def _rec(*args: Any, _orig: Callable = orig, _name: str = name, **kwargs: Any) -> Any:
                calls.append(f"close:{_name}")
                return _orig(*args, **kwargs)

            setattr(env.gateway, name, _rec)
        started = threading.Event()
        release = threading.Event()

        def _blocker():
            started.set()
            release.wait(timeout=5)

        blocker = asyncio.ensure_future(run_gateway_io(_blocker))
        await asyncio.to_thread(started.wait, 5)  # 先占住唯一网关线程

        def _backlog(i: int):
            calls.append(f"backlog:{i}")

        backlog = [asyncio.ensure_future(run_gateway_io(_backlog, i)) for i in range(3)]
        await asyncio.sleep(0.01)  # 普通任务先排入队列
        close = asyncio.ensure_future(close_position(env.deps, "BTC_USDT"))
        release.set()
        await asyncio.gather(blocker, close, *backlog)
        assert close.result().outcome.risk_verdict == "allow"
        close_idx = [i for i, c in enumerate(calls) if c.startswith("close:")]
        backlog_idx = [i for i, c in enumerate(calls) if c.startswith("backlog:")]
        assert close_idx and backlog_idx
        assert max(close_idx) < min(backlog_idx)
    finally:
        await env.db.close()


class _PagedStubGateway:
    """分页存根：第一页慢（0.3s）且返回满页 100 条，后续页快；记录执行顺序。"""

    def __init__(self) -> None:
        """初始化存根：空执行日志与页调用计数。

        参数：无
        返回：
            None，就地初始化实例属性
        """
        self.log: list[str] = []

    def list_orders(self, *args: Any, **kwargs: Any) -> list[Any]:
        """按 offset 返回伪分页：第一页（offset=0）慢且满页，其余页一条收尾。

        参数：
            args: tuple，位置参数（忽略）
            kwargs: dict，含 offset（分页偏移）
        返回：
            list[Any]：本页伪订单列表
        """
        offset = kwargs.get("offset", 0)
        self.log.append(f"list_orders:{offset}")
        if offset == 0:
            time.sleep(0.3)  # 慢第一页：制造 HIGH 任务提交窗口
            return [SimpleNamespace(id=str(i)) for i in range(100)]
        return [SimpleNamespace(id="last")]

    def place_order(self, req: Any) -> Any:
        """记录一次下单调用（用于 HIGH 任务执行顺序断言）。

        参数：
            req: Any，订单请求（忽略）
        返回：
            Any：伪订单结果
        """
        self.log.append("place_order")
        return SimpleNamespace(id="x")


async def test_high_priority_task_preempts_between_pages():
    """验证慢分页进行中提交的高优先级任务在下一页拉取之前执行，而非等待整段分页。

    参数：无

    返回：
        None，断言 HIGH 下单的执行顺序先于第二页 list_orders（逐页卸载 +
        优先级队列让安全操作页间插队）
    """
    stub = _PagedStubGateway()
    pagination = asyncio.ensure_future(_list_all_open_orders(stub))
    await asyncio.sleep(0.1)  # 第一页（慢 0.3s）已进入 executor 执行
    high = asyncio.ensure_future(run_gateway_io(stub.place_order, None, priority=PRIORITY_HIGH))
    await asyncio.gather(pagination, high)
    assert stub.log.index("place_order") < stub.log.index("list_orders:100")


async def test_concurrent_update_tpsl_leave_single_group(tmp_path):
    """验证两个并发 update_tpsl 串行生效（后写覆盖），不会留下两套新保护单。

    参数：
        tmp_path: Path，pytest 临时目录夹具

    返回：
        None，断言两次更新均成功、最终该合约只剩一个止损单且触发价属于后生效者
    """
    env = await _make_tools(tmp_path)
    try:
        env.gateway.place_order(OrderRequest(contract="BTC_USDT", size=Decimal(1)))
        env.gateway.create_tpsl_order(
            TpslOrder(
                id="",
                contract="BTC_USDT",
                direction=1,
                kind="stop_loss",
                trigger_price=Decimal(56000),
            )
        )
        first = update_tpsl(env.deps, {"contract": "BTC_USDT", "stop_loss_price": 58000})
        second = update_tpsl(env.deps, {"contract": "BTC_USDT", "stop_loss_price": 57000})
        out1, out2 = await asyncio.gather(first, second)
        assert out1.risk_verdict == "allow" and "止损已更新" in out1.text, out1.text
        assert out2.risk_verdict == "allow" and "止损已更新" in out2.text, out2.text
        orders = env.gateway.list_tpsl_orders("BTC_USDT")
        assert len(orders) == 1
        assert orders[0].trigger_price in (Decimal(58000), Decimal(57000))
    finally:
        await env.db.close()


async def test_manual_close_does_not_interleave_tpsl_swap(tmp_path):
    """验证 manual close 不会插入 TPSL 交换中段：平仓单只在整个交换完成后执行。

    参数：
        tmp_path: Path，pytest 临时目录夹具

    返回：
        None，断言事件顺序为 创建新保护 → 撤销旧保护 → 平仓下单（交换段连续，
        高优 close 排在原子交换之后，不存在半完成交换后的悬挂保护单）
    """
    env = await _make_tools(tmp_path)
    try:
        env.gateway.place_order(OrderRequest(contract="BTC_USDT", size=Decimal(1)))
        env.gateway.create_tpsl_order(
            TpslOrder(
                id="",
                contract="BTC_USDT",
                direction=1,
                kind="stop_loss",
                trigger_price=Decimal(56000),
            )
        )
        events: list[str] = []
        orig_create = env.gateway.create_tpsl_order
        orig_cancel = env.gateway.cancel_tpsl_order
        orig_place = env.gateway.place_order

        def _create(order: TpslOrder) -> TpslOrder:
            events.append(f"create:{order.trigger_price}")
            time.sleep(0.2)  # 拉长交换窗口，给 manual close 制造插入机会
            return orig_create(order)

        def _cancel(order_id: str) -> None:
            events.append(f"cancel_tpsl:{order_id}")
            orig_cancel(order_id)

        def _place(req: OrderRequest) -> Any:
            events.append(f"place:{req.size}")
            return orig_place(req)

        env.gateway.create_tpsl_order = _create
        env.gateway.cancel_tpsl_order = _cancel
        env.gateway.place_order = _place
        update = asyncio.ensure_future(
            update_tpsl(env.deps, {"contract": "BTC_USDT", "stop_loss_price": 58000})
        )
        await asyncio.sleep(0.1)  # 交换进行中（create 慢 0.2s）
        close = asyncio.ensure_future(close_position(env.deps, "BTC_USDT"))
        out_update, out_close = await asyncio.gather(update, close)
        assert out_update.risk_verdict == "allow", out_update.text
        assert out_close.outcome.risk_verdict == "allow", out_close.outcome.text
        create_idx = events.index("create:58000")
        cancel_idx = next(i for i, e in enumerate(events) if e.startswith("cancel_tpsl:"))
        close_idx = events.index("place:0")
        assert create_idx < cancel_idx < close_idx
    finally:
        await env.db.close()


# ---------- PR #84 第二轮评审回归：复合辅助线程亲和 / TPSL 持仓核验 ----------


async def test_amend_direction_helper_inline_for_paper_with_concurrent_on_price():
    """验证 async _amend_direction 的内部读取仍按 paper 方法各自命中内联，与撮合共线程。

    参数：无

    返回：
        None，断言 executor 被占住且并发 on_price 注入时，_amend_direction 直接
        await 仍在事件循环线程完成（内部 list_positions/list_orders 命中 paper
        内联标记不进 executor，与 on_price 保持同线程）
    """
    paper = make_gateway()
    buy(paper, 1)  # 持有多仓，供 _amend_direction 读取
    started = threading.Event()
    release = threading.Event()

    def _blocker():
        started.set()
        release.wait(timeout=5)

    blocker = asyncio.ensure_future(run_gateway_io(_blocker))
    await asyncio.to_thread(started.wait, 5)  # 先占住唯一网关线程
    try:

        async def _feed():
            for i in range(100):
                paper.on_price("BTC_USDT", Decimal(100 + i % 5), Decimal("99.9"), Decimal("100.1"))
                await asyncio.sleep(0)

        feed = asyncio.ensure_future(_feed())
        try:
            for _ in range(50):
                is_close, size = await asyncio.wait_for(
                    _amend_direction(paper, "BTC_USDT", "oid", Decimal(1)),
                    timeout=1,
                )
                assert (is_close, size) == (False, Decimal(1))  # 同向改单：新敞口
        finally:
            await feed
    finally:
        release.set()
        await blocker


async def test_account_equity_helper_inline_for_paper():
    """验证 async _account_equity 的内部读取仍按 paper 方法各自命中内联标记。

    参数：无

    返回：
        None，断言 executor 被占住时 _account_equity 直接 await 仍在事件循环
        线程完成且估值正确（内部 get_account/list_positions 命中 paper 内联
        标记不进 executor）
    """
    paper = make_gateway()
    started = threading.Event()
    release = threading.Event()

    def _blocker():
        started.set()
        release.wait(timeout=5)

    blocker = asyncio.ensure_future(run_gateway_io(_blocker))
    await asyncio.to_thread(started.wait, 5)
    try:
        equity = await asyncio.wait_for(_account_equity(paper), timeout=1)
        assert equity == Decimal("10000")  # 初始权益，无持仓保证金与浮盈
    finally:
        release.set()
        await blocker


async def test_paper_get_tickers_inline_only_without_ticker_provider():
    """验证 paper 的 get_tickers 按实例动态判定：无真实 provider 时内联，有则卸载。

    参数：无

    返回：
        None，断言 provider=None 时 executor 被占住仍内联完成；注入 provider 后
        _is_inline_call 判定为卸载（真实 REST 不得阻塞事件循环）
    """
    paper = make_gateway()  # ticker_provider=None：get_tickers 由内存快照合成
    assert _is_inline_call(paper.get_tickers, ())
    started = threading.Event()
    release = threading.Event()

    def _blocker():
        started.set()
        release.wait(timeout=5)

    blocker = asyncio.ensure_future(run_gateway_io(_blocker))
    await asyncio.to_thread(started.wait, 5)
    try:
        tickers = await asyncio.wait_for(run_gateway_io(paper.get_tickers), timeout=1)
        assert [t.contract for t in tickers] == ["BTC_USDT"]
    finally:
        release.set()
        await blocker
    paper._ticker_provider = lambda: []  # paper+真实行情装配形态：转发真实 REST
    assert not _is_inline_call(paper.get_tickers, ())


async def test_update_tpsl_position_closed_before_swap_reports_not_updated(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """验证 TPSL 交换前持仓被高优平仓作废时，不建任何新保护单且不报"已更新"。

    参数：
        tmp_path: Path，pytest 临时目录夹具
        monkeypatch: pytest.MonkeyPatch，拦截 run_gateway_io 模拟平仓插队

    返回：
        None，断言交换事务开头重读持仓发现已平仓，返回非成功文案且保护单为空
        （修复前使用风控 await 窗口前的旧快照，在已平仓合约上建单并报"止损已更新"）
    """
    env = await _make_tools(tmp_path)
    try:
        env.gateway.positions["BTC_USDT"] = _long_position("2")
        real_run = tool_tpsl.run_gateway_io

        async def _close_before_swap(fn, /, *args, **kwargs):
            if getattr(fn, "__name__", "") == "_swap_tpsl_group":
                env.gateway.positions.pop("BTC_USDT", None)  # 风控 await 窗口内平仓完成
            return await real_run(fn, *args, **kwargs)

        monkeypatch.setattr(tool_tpsl, "run_gateway_io", _close_before_swap)
        out = await update_tpsl(env.deps, {"contract": "BTC_USDT", "stop_loss_price": 58000})
        assert "止损已更新" not in out.text
        assert "未更新" in out.text
        assert env.gateway.list_tpsl_orders("BTC_USDT") == []
    finally:
        await env.db.close()


def _pos(contract: str) -> Position:
    """构造一个最小持仓对象（1 张多仓，固定价格）。

    参数：
        contract: str，合约名

    返回：
        Position：仅填模型必填项的持仓对象（无保护单触发价）
    """
    return Position(
        contract=contract,
        size=Decimal(1),
        entry_price=Decimal(100),
        mark_price=Decimal(100),
        liq_price=Decimal(50),
        leverage=Decimal(1),
        margin=Decimal(100),
        unrealised_pnl=Decimal(0),
    )


class _SlowTpslStubGateway:
    """两持仓存根：BTC 的保护单查询慢 0.3s，其余调用即刻返回；记录执行顺序。"""

    def __init__(self) -> None:
        """初始化调用顺序日志。

        参数：无

        返回：
            None，就地初始化 log 列表
        """
        self.log: list[str] = []

    def list_positions(self) -> list[Position]:
        """返回 BTC/ETH 两条持仓。

        参数：无

        返回：
            list[Position]：两条伪持仓
        """
        self.log.append("list_positions")
        return [_pos("BTC_USDT"), _pos("ETH_USDT")]

    def list_tpsl_orders(self, contract: str) -> list[TpslOrder]:
        """记录保护单查询；BTC 查询阻塞 0.3s（制造 HIGH 插队窗口）。

        参数：
            contract: str，合约名

        返回：
            list[TpslOrder]：空列表（无保护单）
        """
        self.log.append(f"tpsl:{contract}")
        if contract == "BTC_USDT":
            time.sleep(0.3)
        return []

    def place_order(self, req: Any) -> Any:
        """记录一次下单调用（HIGH 插队顺序断言用）。

        参数：
            req: Any，订单请求（忽略）

        返回：
            Any：伪订单结果
        """
        self.log.append("place_order")
        return SimpleNamespace(id="x")


async def test_high_priority_interleaves_between_tpsl_enrichment_subrequests():
    """验证持仓 TPSL 补全的逐合约子请求各自独立调度：HIGH 安全操作可在合约间隙插队。

    参数：无

    返回：
        None，断言 HIGH 下单在 BTC 慢保护单查询之后、ETH 查询之前执行——补全
        不再是一个整段复合任务（修复前 list_positions 内部串行 N+1，HIGH 只能
        等全部合约查完）
    """
    stub = _SlowTpslStubGateway()
    read = asyncio.ensure_future(read_positions_with_tpsl(stub))
    await asyncio.sleep(0.1)  # BTC 的慢保护单查询已进入 executor
    high = asyncio.ensure_future(run_gateway_io(stub.place_order, None, priority=PRIORITY_HIGH))
    await asyncio.gather(read, high)
    assert stub.log == ["list_positions", "tpsl:BTC_USDT", "place_order", "tpsl:ETH_USDT"]


async def test_manual_close_succeeds_when_tpsl_query_fails(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """验证安全路径零 TPSL 依赖：保护单查询接口故障时人工平仓照常放行成交。

    参数：
        tmp_path: Path，pytest 临时目录夹具
        monkeypatch: pytest.MonkeyPatch，打桩 list_tpsl_orders 抛传输异常

    返回：
        None，断言平仓风控放行——平仓链路只读裸持仓，不经任何保护单查询
        （修复前 list_positions 内部复合 TPSL 查询，保护单接口超时直接拖死平仓）
    """
    env = await _make_tools(tmp_path)
    try:
        env.gateway.positions["BTC_USDT"] = _long_position("2")

        def _boom(contract: str) -> list[TpslOrder]:
            raise GatewayError("保护单查询超时", label="TRANSPORT_UNKNOWN")

        monkeypatch.setattr(env.gateway, "list_tpsl_orders", _boom)
        result = await close_position(env.deps, "BTC_USDT")
        assert result.outcome.risk_verdict == "allow", result.outcome.text
    finally:
        await env.db.close()


class _FailSoftTpslStubGateway:
    """两持仓存根：BTC 保护单查询抛传输异常，ETH 返回一个止损单。"""

    def list_positions(self) -> list[Position]:
        """返回 BTC/ETH 两条持仓。

        参数：无

        返回：
            list[Position]：两条伪持仓
        """
        return [_pos("BTC_USDT"), _pos("ETH_USDT")]

    def list_tpsl_orders(self, contract: str) -> list[TpslOrder]:
        """按合约返回保护单：BTC 抛传输异常，ETH 返回一个止损单。

        参数：
            contract: str，合约名

        返回：
            list[TpslOrder]：ETH 合约返回单个止损单

        异常：
            GatewayError：BTC 合约查询固定抛传输异常
        """
        if contract == "BTC_USDT":
            raise GatewayError("保护单查询超时", label="TRANSPORT_UNKNOWN")
        return [
            TpslOrder(
                id="1",
                contract=contract,
                direction=1,
                kind="stop_loss",
                trigger_price=Decimal(58000),
            )
        ]


async def test_read_positions_with_tpsl_fail_soft_per_contract():
    """验证 TPSL 补全的单合约失败降级：仅该合约止损/止盈为 None，不拖垮整体读取。

    参数：无

    返回：
        None，断言 BTC 查询异常时其 stop_loss_price 为 None 且整体不抛异常，
        ETH 的止损价正常回填
    """
    positions = await read_positions_with_tpsl(_FailSoftTpslStubGateway())
    by_contract = {p.contract: p for p in positions}
    assert by_contract["BTC_USDT"].stop_loss_price is None
    assert by_contract["ETH_USDT"].stop_loss_price == Decimal(58000)


class _SlowContractMetaStubGateway:
    """合约元数据存根：BTC 元数据查询慢 0.3s；记录执行顺序（HIGH 插队断言用）。"""

    def __init__(self) -> None:
        """初始化调用顺序日志。

        参数：无

        返回：
            None，就地初始化 log 列表
        """
        self.log: list[str] = []

    def get_contract(self, contract: str) -> SimpleNamespace:
        """记录元数据查询；BTC 查询阻塞 0.3s（制造 HIGH 插队窗口）。

        参数：
            contract: str，合约名

        返回：
            SimpleNamespace：含 mark_price/quanto_multiplier 的伪合约元数据
        """
        self.log.append(f"get_contract:{contract}")
        if contract == "BTC_USDT":
            time.sleep(0.3)
        return SimpleNamespace(mark_price=Decimal(100), quanto_multiplier=Decimal("0.001"))

    def place_order(self, req: Any) -> Any:
        """记录一次下单调用（HIGH 插队顺序断言用）。

        参数：
            req: Any，订单请求（忽略）

        返回：
            Any：伪订单结果
        """
        self.log.append("place_order")
        return SimpleNamespace(id="x")


async def test_high_priority_interleaves_between_snapshot_meta_reads():
    """验证风控快照的逐合约元数据读取各自独立调度：HIGH 安全操作可在合约间隙插队。

    参数：无

    返回：
        None，断言 HIGH 下单在 BTC 慢元数据查询之后、ETH 查询之前执行
        （与 list_positions 裸读同一原则：N 次串行读取不打包成单个复合任务）
    """
    stub = _SlowContractMetaStubGateway()
    positions = [_pos("BTC_USDT"), _pos("ETH_USDT")]
    read = asyncio.ensure_future(position_snapshots(stub, positions))
    await asyncio.sleep(0.1)  # BTC 的慢元数据查询已进入 executor
    high = asyncio.ensure_future(run_gateway_io(stub.place_order, None, priority=PRIORITY_HIGH))
    await asyncio.gather(read, high)
    assert stub.log == ["get_contract:BTC_USDT", "place_order", "get_contract:ETH_USDT"]


class _SlowAccountStubGateway:
    """账户/持仓存根：get_account 慢 0.3s，list_positions 即刻返回；记录执行顺序。"""

    def __init__(self) -> None:
        """初始化调用顺序日志。

        参数：无

        返回：
            None，就地初始化 log 列表
        """
        self.log: list[str] = []

    def get_account(self) -> SimpleNamespace:
        """记录账户查询并阻塞 0.3s（制造 HIGH 插队窗口）。

        参数：无

        返回：
            SimpleNamespace：伪账户（available=9000、unrealised_pnl=100）
        """
        self.log.append("get_account")
        time.sleep(0.3)
        return SimpleNamespace(available=Decimal(9000), unrealised_pnl=Decimal(100))

    def list_positions(self) -> list[Position]:
        """记录持仓查询并返回空列表。

        参数：无

        返回：
            list[Position]：空列表
        """
        self.log.append("list_positions")
        return []

    def place_order(self, req: Any) -> Any:
        """记录一次下单调用（HIGH 插队顺序断言用）。

        参数：
            req: Any，订单请求（忽略）

        返回：
            Any：伪订单结果
        """
        self.log.append("place_order")
        return SimpleNamespace(id="x")


async def test_high_priority_interleaves_between_account_equity_reads():
    """验证 _account_equity 的账户/持仓两次读取各自独立调度：HIGH 可在间隙插队。

    参数：无

    返回：
        None，断言 HIGH 下单在慢账户读取之后、持仓读取之前执行，且权益估值
        正确（修复前两读打包成一个 NORMAL 任务，HIGH 必须等两个 REST 子请求
        全部完成）
    """
    stub = _SlowAccountStubGateway()
    read = asyncio.ensure_future(_account_equity(stub))
    await asyncio.sleep(0.1)  # 慢账户读取已进入 executor
    high = asyncio.ensure_future(run_gateway_io(stub.place_order, None, priority=PRIORITY_HIGH))
    equity, _ = await asyncio.gather(read, high)
    assert equity == Decimal(9100)
    assert stub.log == ["get_account", "place_order", "list_positions"]


async def test_cancelled_dispatched_mutation_triggers_orphan_handler(
    monkeypatch: pytest.MonkeyPatch,
):
    """验证已 dispatch 的写请求被取消时触发孤儿写兜底，成功结果不静默丢失。

    参数：
        monkeypatch: pytest.MonkeyPatch，替换模块级孤儿写回调

    返回：
        None，断言阻塞写进入 executor 后取消调用方：孤儿回调收到写操作名，
        且线程内的写仍自行跑完——交易所可能已执行，系统必须 fail-closed 而
        不是假装没发生（PR #84 评审 P1）
    """
    orphaned: list[str] = []
    monkeypatch.setattr("src.gateway.async_io._orphan_write_handler", orphaned.append)
    entered = threading.Event()
    release = threading.Event()
    done = threading.Event()

    def _write() -> str:
        entered.set()
        release.wait(timeout=5)
        done.set()
        return "ok"

    stub = SimpleNamespace(place_order=_write)  # 无内联标记 → 走 executor
    task = asyncio.ensure_future(run_gateway_io(stub.place_order, mutation=True))
    await asyncio.to_thread(entered.wait, 5)  # 确认写请求已 dispatch 进 executor
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    await asyncio.to_thread(done.wait, 5)  # 线程内的写请求自行跑完（结果无人接收）
    await asyncio.sleep(0)  # 消费协程回收任务
    assert orphaned == ["_write"]


async def test_cancelled_queued_mutation_does_not_trigger_orphan_handler(
    monkeypatch: pytest.MonkeyPatch,
):
    """验证仍在队列（未 dispatch）的写请求可安全撤回，不触发孤儿写兜底。

    参数：
        monkeypatch: pytest.MonkeyPatch，替换模块级孤儿写回调

    返回：
        None，断言 executor 被占住时取消排队中的写：回调不触发（请求从未
        到达交易所，撤回无副作用）
    """
    orphaned: list[str] = []
    monkeypatch.setattr("src.gateway.async_io._orphan_write_handler", orphaned.append)
    entered = threading.Event()
    release = threading.Event()

    def _blocker() -> None:
        entered.set()
        release.wait(timeout=5)

    blocker = asyncio.ensure_future(run_gateway_io(_blocker))
    await asyncio.to_thread(entered.wait, 5)  # 占住唯一网关线程
    stub = SimpleNamespace(place_order=lambda: "ok")
    task = asyncio.ensure_future(run_gateway_io(stub.place_order, mutation=True))
    await asyncio.sleep(0.05)  # 已入队但仍在排队（未 dispatch）
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    await blocker
    assert orphaned == []


async def test_cancelled_dispatched_read_does_not_trigger_orphan_handler(
    monkeypatch: pytest.MonkeyPatch,
):
    """验证只读请求（未标 mutation）dispatch 后取消不触发孤儿写兜底。

    参数：
        monkeypatch: pytest.MonkeyPatch，替换模块级孤儿写回调

    返回：
        None，断言阻塞读进入 executor 后取消调用方：回调不触发（读操作无
        交易所副作用，丢弃结果安全）
    """
    orphaned: list[str] = []
    monkeypatch.setattr("src.gateway.async_io._orphan_write_handler", orphaned.append)
    entered = threading.Event()
    release = threading.Event()

    def _read() -> str:
        entered.set()
        release.wait(timeout=5)
        return "ok"

    task = asyncio.ensure_future(run_gateway_io(_read))  # mutation 默认 False
    await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    await asyncio.sleep(0.1)
    assert orphaned == []


async def test_close_and_bump_epoch_bumps_unless_definite_rejection(tmp_path):
    """验证平仓代际包装：成功上调代际，仅交易所明确拒绝（GatewayError）不上调。

    参数：
        tmp_path: Path，pytest 临时目录夹具

    返回：
        None，断言成功平仓后代际 +1；带 label 的明确拒绝时代际保持不变
        （平仓确定未发生）
    """
    env = await _make_tools(tmp_path)
    try:
        env.gateway.positions["BTC_USDT"] = _long_position("2")
        epochs: dict[str, int] = {}
        req = OrderRequest(contract="BTC_USDT", size=Decimal(0), close=True)
        result = _close_and_bump_epoch(env.gateway, req, epochs, "BTC_USDT")
        assert result.id
        assert epochs["BTC_USDT"] == 1

        def _reject(_req: OrderRequest) -> None:
            raise GatewayError("明确拒绝", label="POSITION_NOT_FOUND")

        with pytest.raises(GatewayError):
            _close_and_bump_epoch(SimpleNamespace(place_order=_reject), req, epochs, "BTC_USDT")
        assert epochs["BTC_USDT"] == 1
    finally:
        await env.db.close()


async def test_close_and_bump_epoch_bumps_on_state_unknown(tmp_path):
    """验证平仓状态未知同样上调代际：远端可能已平仓，持旧代际的增仓必须中止。

    参数：
        tmp_path: Path，pytest 临时目录夹具

    返回：
        None，断言远端已执行平仓副作用但抛 OrderStateUnknown 时：代际 +1，
        持旧代际的增仓被拦下返回 None，最终仓位为空不重开（PR #84 评审 P1）
    """
    env = await _make_tools(tmp_path)
    try:
        env.gateway.positions["BTC_USDT"] = _long_position("2")
        epochs: dict[str, int] = {}
        req = OrderRequest(contract="BTC_USDT", size=Decimal(0), close=True)

        def _unknown(_req: OrderRequest) -> None:
            env.gateway.place_order(_req)  # 远端实际已平仓（副作用已发生）
            raise OrderStateUnknown("超时且回查失败")

        with pytest.raises(OrderStateUnknown):
            _close_and_bump_epoch(SimpleNamespace(place_order=_unknown), req, epochs, "BTC_USDT")
        assert epochs["BTC_USDT"] == 1
        skipped = _place_unless_close_intervened(
            env.gateway,
            OrderRequest(contract="BTC_USDT", size=Decimal(1), stop_loss_price=Decimal(58000)),
            epochs,
            "BTC_USDT",
            0,
        )
        assert skipped is None
        remaining = [p for p in env.gateway.list_positions() if p.contract == "BTC_USDT"]
        assert all(p.size == 0 for p in remaining)
    finally:
        await env.db.close()


async def test_place_unless_close_intervened_checks_epoch(tmp_path):
    """验证增仓代际检查：代际一致正常下单，代际已变返回 None 且不下单。

    参数：
        tmp_path: Path，pytest 临时目录夹具

    返回：
        None，断言代际一致返回订单结果并开仓；代际 +1 后返回 None 且持仓不变
    """
    env = await _make_tools(tmp_path)
    try:
        epochs = {"BTC_USDT": 0}
        req = OrderRequest(contract="BTC_USDT", size=Decimal(1), stop_loss_price=Decimal(58000))
        placed = _place_unless_close_intervened(env.gateway, req, epochs, "BTC_USDT", 0)
        assert placed is not None
        size_after = sum(p.size for p in env.gateway.list_positions() if p.contract == "BTC_USDT")
        assert size_after == 1
        epochs["BTC_USDT"] = 1  # 模拟人工平仓已介入
        skipped = _place_unless_close_intervened(env.gateway, req, epochs, "BTC_USDT", 0)
        assert skipped is None
        size_final = sum(p.size for p in env.gateway.list_positions() if p.contract == "BTC_USDT")
        assert size_final == 1
    finally:
        await env.db.close()


async def test_high_priority_close_before_stale_open_aborts_open(tmp_path):
    """复刻评审场景：高优人工平仓抢在旧增仓写之前执行，增仓必须被代际拦下。

    参数：
        tmp_path: Path，pytest 临时目录夹具

    返回：
        None，断言 executor 排队时 HIGH 平仓先 dispatch：代际 +1 后 NORMAL
        增仓检查到代际变化返回 None（不下单），最终仓位为空——不会"平完又开"
        （PR #84 评审 P1）
    """
    env = await _make_tools(tmp_path)
    try:
        env.gateway.positions["BTC_USDT"] = _long_position("2")
        epoch0 = env.deps.close_epochs.get("BTC_USDT", 0)
        entered = threading.Event()
        release = threading.Event()

        def _blocker() -> None:
            entered.set()
            release.wait(timeout=5)

        blocker = asyncio.ensure_future(run_gateway_io(_blocker))
        await asyncio.to_thread(entered.wait, 5)  # 占住唯一网关线程
        req_open = OrderRequest(
            contract="BTC_USDT", size=Decimal(1), stop_loss_price=Decimal(58000)
        )
        req_close = OrderRequest(contract="BTC_USDT", size=Decimal(0), close=True)
        open_fut = asyncio.ensure_future(
            run_gateway_io(
                _place_unless_close_intervened,
                env.gateway,
                req_open,
                env.deps.close_epochs,
                "BTC_USDT",
                epoch0,
                mutation=True,
            )
        )
        close_fut = asyncio.ensure_future(
            run_gateway_io(
                _close_and_bump_epoch,
                env.gateway,
                req_close,
                env.deps.close_epochs,
                "BTC_USDT",
                priority=PRIORITY_HIGH,
                mutation=True,
            )
        )
        await asyncio.sleep(0.05)  # 两个写请求都已入队排队
        release.set()
        await blocker
        open_result, close_result = await asyncio.gather(open_fut, close_fut)
        assert open_result is None  # 平仓先执行、代际已变：旧增仓写被拦下
        assert close_result is not None
        remaining = [p for p in env.gateway.list_positions() if p.contract == "BTC_USDT"]
        assert all(p.size == 0 for p in remaining)
    finally:
        await env.db.close()


async def test_place_with_rollback_aborts_when_close_intervened(tmp_path):
    """验证增仓单捕获旧代际后人工平仓落地：下单中止返回 deny，仓位不重开。

    参数：
        tmp_path: Path，pytest 临时目录夹具

    返回：
        None，断言 _place_with_rollback 返回"人工平仓介入"deny 文案，
        最终仓位保持为空
    """
    env = await _make_tools(tmp_path)
    try:
        env.gateway.positions["BTC_USDT"] = _long_position("2")
        epoch0 = env.deps.close_epochs.get("BTC_USDT", 0)
        cr = await close_position(env.deps, "BTC_USDT")  # 人工平仓先落地（代际 +1）
        assert cr.outcome.risk_verdict == "allow", cr.outcome.text
        req = OrderRequest(contract="BTC_USDT", size=Decimal(1), stop_loss_price=Decimal(58000))
        placed = await _place_with_rollback(
            env.deps, req, None, leverage_modified=False, close_epoch=epoch0
        )
        assert isinstance(placed, ToolOutcome)
        assert placed.risk_verdict == "deny"
        assert "人工平仓" in placed.text
        remaining = [p for p in env.gateway.list_positions() if p.contract == "BTC_USDT"]
        assert all(p.size == 0 for p in remaining)
    finally:
        await env.db.close()


async def test_place_unless_reset_intervened_skips(tmp_path):
    """验证重置代际比对：账户重置介入（计数变化）时增仓写返回 None 且不下单。

    参数：
        tmp_path: Path，pytest 临时目录夹具

    返回：
        None，断言捕获锚点后 reset_epoch 上调，包装器返回 None、持仓不变（issue #81）
    """
    env = await _make_tools(tmp_path)
    try:
        resets = env.deps.reset_epoch
        req = OrderRequest(contract="BTC_USDT", size=Decimal(1), stop_loss_price=Decimal(58000))
        placed = _place_unless_close_intervened(
            env.gateway,
            req,
            {"BTC_USDT": 0},
            "BTC_USDT",
            0,
            resets=resets,
            reset0=resets[0],
        )
        assert placed is not None
        resets[0] += 1  # 模拟账户在风控窗口内被重置
        skipped = _place_unless_close_intervened(
            env.gateway,
            req,
            {"BTC_USDT": 0},
            "BTC_USDT",
            0,
            resets=resets,
            reset0=resets[0] - 1,
        )
        assert skipped is None
        size_after = sum(p.size for p in env.gateway.list_positions() if p.contract == "BTC_USDT")
        assert size_after == 1  # 迟到的旧增仓未在新账户上重复开仓
    finally:
        await env.db.close()


async def test_place_with_rollback_aborts_when_reset_intervened(tmp_path):
    """复刻 issue #81 场景：增仓过风控窗口内账户被重置，下单必须中止且不写新账户。

    参数：
        tmp_path: Path，pytest 临时目录夹具

    返回：
        None，断言 _place_with_rollback 返回中止 deny 文案，仓位保持为空
    """
    env = await _make_tools(tmp_path)
    try:
        reset0 = env.deps.reset_epoch[0]
        close_epoch = env.deps.close_epochs.get("BTC_USDT", 0)
        env.deps.reset_epoch[0] += 1  # 模拟 paper 重置发生在风控窗口内
        req = OrderRequest(contract="BTC_USDT", size=Decimal(1), stop_loss_price=Decimal(58000))
        placed = await _place_with_rollback(
            env.deps,
            req,
            None,
            leverage_modified=False,
            close_epoch=close_epoch,
            reset_epoch=env.deps.reset_epoch,
            reset0=reset0,
        )
        assert isinstance(placed, ToolOutcome)
        assert placed.risk_verdict == "deny"
        remaining = [p for p in env.gateway.list_positions() if p.contract == "BTC_USDT"]
        assert all(p.size == 0 for p in remaining)
    finally:
        await env.db.close()


async def test_amend_unless_close_intervened_checks_epoch(tmp_path):
    """验证改单代际检查：代际一致正常改单，代际已变返回 None 且挂单不变。

    参数：
        tmp_path: Path，pytest 临时目录夹具

    返回：
        None，断言代际一致返回改单结果（剩余量变化）；代际 +1 后返回 None
        且挂单剩余量保持原值（PR #84 评审 P1）
    """
    env = await _make_tools(tmp_path)
    try:
        order_id = await _open_limit_order(env)
        epochs = {"BTC_USDT": 0}
        amended = _amend_unless_close_intervened(
            env.gateway, "BTC_USDT", order_id, None, Decimal(3), epochs, 0
        )
        assert amended is not None
        order = next(o for o in env.gateway.list_orders("BTC_USDT", "open") if o.id == order_id)
        assert order.left == 3
        epochs["BTC_USDT"] = 1  # 模拟人工平仓已介入
        skipped = _amend_unless_close_intervened(
            env.gateway, "BTC_USDT", order_id, None, Decimal(5), epochs, 0
        )
        assert skipped is None
        order = next(o for o in env.gateway.list_orders("BTC_USDT", "open") if o.id == order_id)
        assert order.left == 3
    finally:
        await env.db.close()


async def test_amend_order_aborts_when_close_intervenes(tmp_path):
    """复刻评审场景：增仓改单风控窗口内高优人工平仓落地，改单必须被代际拦下。

    参数：
        tmp_path: Path，pytest 临时目录夹具

    返回：
        None，断言改单工具返回"人工平仓介入"deny、挂单剩余量不变、
        仓位保持为空——不会"平完又被旧改单重开"（PR #84 评审 P1）
    """
    env = await _make_tools(tmp_path)
    try:
        order_id = await _open_limit_order(env)
        env.gateway.positions["BTC_USDT"] = _long_position("2")
        env.gateway.list_positions = _slow(env.gateway.list_positions)  # 0.3s 制造风控窗口
        amend_task = asyncio.ensure_future(
            env.registry.execute(
                "amend_order",
                {"contract": "BTC_USDT", "order_id": order_id, "price": 59500},
            )
        )
        await asyncio.sleep(0.1)  # 改单已捕获代际锚点，正在慢读风控
        cr = await close_position(env.deps, "BTC_USDT")  # HIGH 平仓插队，代际 +1
        assert cr.outcome.risk_verdict == "allow", cr.outcome.text
        out = await amend_task
        assert out.risk_verdict == "deny"
        assert "人工平仓" in out.text
        order = next(o for o in env.gateway.list_orders("BTC_USDT", "open") if o.id == order_id)
        assert order.left == 1  # 挂单未被改动
        remaining = [p for p in env.gateway.list_positions() if p.contract == "BTC_USDT"]
        assert all(p.size == 0 for p in remaining)
    finally:
        await env.db.close()


async def test_cancelled_dispatched_not_started_mutation_withdraws_cleanly(
    monkeypatch: pytest.MonkeyPatch,
):
    """验证"已提交 executor、worker 未开始执行"窗口内取消：撤回成功不触发兜底。

    参数：
        monkeypatch: pytest.MonkeyPatch，替换模块级孤儿写回调与 executor

    返回：
        None，断言调度器已 dispatch（concurrent Future 已写入探针）但任务
        从未开始执行时取消：cf.cancel() 撤回成功——写函数从未运行、不触发
        孤儿写回调（PR #84 评审 P1：submit→worker-start 竞态）
    """
    orphaned: list[str] = []
    monkeypatch.setattr("src.gateway.async_io._orphan_write_handler", orphaned.append)

    class _HeldExecutor:
        """只收不跑的 executor：submit 记录任务但永不执行，屏障式模拟"已提交未开始"。"""

        def __init__(self) -> None:
            self.submitted: list[Future] = []

        def submit(self, fn: Callable) -> Future:
            fut: Future = Future()
            self.submitted.append(fut)
            return fut

    held = _HeldExecutor()
    monkeypatch.setattr("src.gateway.async_io._EXECUTOR", held)
    ran = threading.Event()

    def _write() -> None:
        ran.set()

    stub = SimpleNamespace(place_order=_write)
    task = asyncio.ensure_future(run_gateway_io(stub.place_order, mutation=True))
    await asyncio.sleep(0.1)  # 调度器已 pop 并 submit（cf 已写入探针）
    assert len(held.submitted) == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.05)
    assert held.submitted[0].cancelled()  # cf.cancel() 撤回成功
    assert orphaned == []  # 请求从未到达"交易所"：安全撤回，无需兜底
    assert not ran.is_set()


async def test_withdrawn_mutation_drain_keeps_consuming_queued_tasks():
    """验证撤回成功后消费协程存活：已排队的后续任务无需新提交即完成。

    参数：无

    返回：
        None，断言：单线程 executor 被 blocker 占住时，mutation A 已 dispatch
        （concurrent Future 已写入探针）但未执行即被安全撤回（cf.cancel() 成功）；
        消费协程不因 wrap_future 抛出的 CancelledError 死亡——释放 blocker 后
        仍排队的 B 正常完成（PR #84 评审 P1：撤回杀死 _drain 会让队列中可能的
        HIGH 人工平仓永久悬挂）
    """
    blocker = threading.Event()
    a_ran = threading.Event()
    executor_blocker = _EXECUTOR.submit(blocker.wait)  # 占住唯一 worker 线程
    try:
        scheduler = _scheduler()

        def _write_a() -> None:
            a_ran.set()

        def _read_b() -> str:
            return "b"

        fut_a, probe_a = scheduler.submit(_write_a, (), {}, PRIORITY_NORMAL)
        fut_b, _probe_b = scheduler.submit(_read_b, (), {}, PRIORITY_NORMAL)
        for _ in range(1000):  # 等调度器把 A 提交进 executor 队列（worker 被 blocker 占住）
            if probe_a.cf is not None:
                break
            await asyncio.sleep(0)
        assert probe_a.cf is not None, "A 应已 dispatch 到 executor"
        withdrawn = probe_a.cf.cancel()  # 模拟调用方取消分支：worker 未开始，撤回成功
        fut_a.cancel()
        assert withdrawn
        blocker.set()  # 释放 worker
        assert await asyncio.wait_for(fut_b, 2) == "b"  # _drain 存活并继续消费队列
        assert not a_ran.is_set()  # A 的写从未执行
    finally:
        blocker.set()
        executor_blocker.result(timeout=5)
