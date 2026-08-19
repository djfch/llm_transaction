"""issue #72 回归：各异步路径注入慢同步网关实现后，事件循环心跳仍持续推进。

覆盖 LLM 下单全链路、手动平仓、决策上下文构建、open orders 分页端点辅助；
另覆盖两处分页查询的总页数上限（PAGINATION_OVERFLOW，防分页异常死循环）。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from src.agent.context import ContextBuilder
from src.agent.manual_close import close_position
from src.gateway.async_io import run_gateway_io
from src.gateway.base import GatewayError
from src.market.candles import CandleCache, ManualPriceSource
from src.market.triggers import TriggerManager
from src.server.routes_trading import _list_all_open_orders
from tests.test_agent_tools_risk import _long_position, _make_tools


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
    orders, ticks = await _run_with_heartbeat(run_gateway_io(_list_all_open_orders, stub))
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
        await run_gateway_io(_list_all_open_orders, stub)
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
        _require_open_order(deps, "BTC_USDT", "999")
    assert excinfo.value.label == "PAGINATION_OVERFLOW"
