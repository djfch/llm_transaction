"""issue #72 回归：各异步路径注入慢同步网关实现后，事件循环心跳仍持续推进。

覆盖 LLM 下单全链路、手动平仓、决策上下文构建、open orders 分页端点辅助；
另覆盖两处分页查询的总页数上限（PAGINATION_OVERFLOW，防分页异常死循环）。
PR #84 评审追加：PaperGateway 账户方法线程亲和（内联不进 executor）、手动平仓
全程高优先级插队、慢分页期间 HIGH 任务页间插入、并发 update_tpsl 原子性
（不留两套新保护）、manual close 不插入 TPSL 交换中段。
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from src.agent.context import ContextBuilder
from src.agent import tool_trading
from src.agent.manual_close import close_position
from src.agent.tool_trading import _amend_direction, update_tpsl
from src.gateway.async_io import PRIORITY_HIGH, _is_inline_call, run_gateway_io
from src.gateway.base import GatewayError, OrderRequest, TpslOrder
from src.market.candles import CandleCache, ManualPriceSource
from src.market.triggers import TriggerManager
from src.server.routes_status import _account_equity
from src.server.routes_trading import _list_all_open_orders
from tests.test_agent_tools_risk import _long_position, _make_tools
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
                "place_order", {"contract": "BTC_USDT", "size": 1, "stop_loss_price": 58000}
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
    """验证 _amend_direction（首参为网关的复合辅助）命中 paper 内联标记，与撮合共线程。

    参数：无

    返回：
        None，断言 executor 被占住且并发 on_price 注入时，_amend_direction 仍在
        事件循环线程内联完成（修复前首参是 deps，被误判卸载到 executor，与
        on_price 跨线程并发读写同一账户）
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
                    run_gateway_io(_amend_direction, paper, "BTC_USDT", "oid", Decimal(1)),
                    timeout=1,
                )
                assert (is_close, size) == (False, Decimal(1))  # 同向改单：新敞口
        finally:
            await feed
    finally:
        release.set()
        await blocker


async def test_account_equity_helper_inline_for_paper():
    """验证 _account_equity（首参为网关的复合辅助）命中 paper 内联标记。

    参数：无

    返回：
        None，断言 executor 被占住时 _account_equity 仍在事件循环线程内联完成
        且估值正确（修复前首参是 deps，被误判卸载到 executor）
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
        equity = await asyncio.wait_for(run_gateway_io(_account_equity, paper), timeout=1)
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
        real_run = tool_trading.run_gateway_io

        async def _close_before_swap(fn, /, *args, **kwargs):
            if getattr(fn, "__name__", "") == "_swap_tpsl_group":
                env.gateway.positions.pop("BTC_USDT", None)  # 风控 await 窗口内平仓完成
            return await real_run(fn, *args, **kwargs)

        monkeypatch.setattr(tool_trading, "run_gateway_io", _close_before_swap)
        out = await update_tpsl(env.deps, {"contract": "BTC_USDT", "stop_loss_price": 58000})
        assert "止损已更新" not in out.text
        assert "未更新" in out.text
        assert env.gateway.list_tpsl_orders("BTC_USDT") == []
    finally:
        await env.db.close()
