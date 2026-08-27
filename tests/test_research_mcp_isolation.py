"""McpSession runner 隔离测试：anyio 泄漏取消/异常组不再越界，真取消语义不变。

复现服务器事故机理：anyio 任务组在网络抖动/超时时把内部取消以 CancelledError
漏进调用协程并反复再抛，曾打穿研报收尾并杀死调度循环。runner 隔离后：
- 工作项自发 CancelledError / ExceptionGroup → 调用方收到 ResearchSourceError
- 调用方真被外部取消（shutdown）→ CancelledError 原样传播
- 会话关闭后 runner 任务必停止（不留后台泄漏）

所有底层会话均 fake，不触真实网络。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from src.research.providers.base import ResearchSourceError
from src.research.providers.mcp_client import McpSession


class _FakeSession:
    """按预设行为表演的底层 MCP 会话（替代 mcp.ClientSession）。"""

    def __init__(self, behavior) -> None:
        """保存表演行为与关闭标记。

        参数：
            behavior: Callable，签名为 (name, args) -> 结果对象；或抛出预定异常

        返回：
            None，就地写入实例属性
        """
        self._behavior = behavior
        self.closed = False

    async def call_tool(self, name: str, args: dict) -> SimpleNamespace:
        """按预设行为返回结果或抛异常。

        参数：
            name: str，工具名
            args: dict，工具调用参数

        返回：
            SimpleNamespace，带 content/is_error 字段的伪调用结果
        """
        return await self._behavior(name, args)

    async def __aexit__(self, *exc_info: object) -> None:
        """标记会话已关闭。

        参数：
            exc_info: object，async with 退出时传入的异常信息，本 fake 忽略

        返回：
            None，就地把 closed 置为 True
        """
        self.closed = True


def _ok_text(text: str) -> SimpleNamespace:
    """构造 is_error=False、文本为 text 的伪调用结果。

    参数：
        text: str，伪 MCP 工具返回文本

    返回：
        SimpleNamespace，带 content/is_error 字段的伪调用结果
    """
    return SimpleNamespace(content=[SimpleNamespace(text=text)], is_error=False)


def _start_detached(session: McpSession, fake: _FakeSession) -> None:
    """不走真实连接，直接给会话装上 runner 与 fake 底层会话（等价 __aenter__ 成功态）。

    参数：
        session: McpSession，待装配的会话
        fake: _FakeSession，替代 mcp.ClientSession 的伪底层会话

    返回：
        None，就地写入会话的 _queue/_runner/_session
    """
    session._queue = asyncio.Queue()
    session._runner = asyncio.create_task(session._runner_loop())
    session._session = fake


async def test_leaked_cancel_converted_to_source_error() -> None:
    """工作项自发 CancelledError（模拟 anyio 泄漏）→ 调用方收到 ResearchSourceError。

    参数：无

    返回：
        None，通过断言验证上述行为，无返回值
    """

    async def leak(name, args):
        raise asyncio.CancelledError("leaked from anyio internals")

    session = McpSession(kind="http", url="http://localhost")
    _start_detached(session, _FakeSession(leak))
    caller = asyncio.current_task()
    with pytest.raises(ResearchSourceError, match="传输层内部取消"):
        await session.call_tool("get_flash")
    assert caller.cancelling() == 0  # 泄漏取消未传染调用方
    assert not session._runner.done()  # runner 存活，会话可继续用
    await session.__aexit__(None, None, None)


async def test_exception_group_converted_to_source_error() -> None:
    """工作项抛 ExceptionGroup（anyio 任务组子失败）→ 调用方收到 ResearchSourceError。

    参数：无

    返回：
        None，通过断言验证上述行为，无返回值
    """

    async def group_fail(name, args):
        raise ExceptionGroup("unhandled errors in a TaskGroup", [OSError("connection reset")])

    session = McpSession(kind="http", url="http://localhost")
    _start_detached(session, _FakeSession(group_fail))
    with pytest.raises(ResearchSourceError, match="connection reset"):
        await session.call_tool("get_flash")
    await session.__aexit__(None, None, None)


async def test_plain_exception_keeps_existing_message() -> None:
    """普通异常维持既有中文口径（MCP 工具 xx 调用失败），不受 runner 影响。

    参数：无

    返回：
        None，通过断言验证上述行为，无返回值
    """

    async def oops(name, args):
        raise OSError("boom")

    session = McpSession(kind="http", url="http://localhost")
    _start_detached(session, _FakeSession(oops))
    with pytest.raises(ResearchSourceError, match="MCP 工具 get_flash 调用失败：boom"):
        await session.call_tool("get_flash")
    await session.__aexit__(None, None, None)


async def test_tool_error_flag_keeps_existing_message() -> None:
    """is_error=True 维持既有口径（MCP 工具 xx 报错）。

    参数：无

    返回：
        None，通过断言验证上述行为，无返回值
    """

    async def flagged(name, args):
        return SimpleNamespace(content=[SimpleNamespace(text="bad params")], is_error=True)

    session = McpSession(kind="http", url="http://localhost")
    _start_detached(session, _FakeSession(flagged))
    with pytest.raises(ResearchSourceError, match="MCP 工具 xx 报错"):
        await session.call_tool("xx")
    await session.__aexit__(None, None, None)


async def test_success_result_passes_through() -> None:
    """正常调用结果原样返回（runner 不改变成功路径语义）。

    参数：无

    返回：
        None，通过断言验证上述行为，无返回值
    """

    async def ok(name, args):
        return _ok_text("快讯内容")

    session = McpSession(kind="http", url="http://localhost")
    _start_detached(session, _FakeSession(ok))
    assert await session.call_tool("get_flash") == "快讯内容"
    await session.__aexit__(None, None, None)


async def test_real_cancellation_propagates_and_runner_stops() -> None:
    """调用方真被外部取消：CancelledError 原样传播；会话关闭后 runner 停止。

    参数：无

    返回：
        None，通过断言验证上述行为，无返回值
    """

    async def hang(name, args):
        await asyncio.sleep(60)

    session = McpSession(kind="http", url="http://localhost")
    _start_detached(session, _FakeSession(hang))
    task = asyncio.create_task(session.call_tool("get_flash"))
    await asyncio.sleep(0.02)  # 让工作项先进入 runner
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await session.__aexit__(None, None, None)
    assert session._runner is None  # 关闭后 runner 必停止，不留后台泄漏


async def test_call_without_session_rejected() -> None:
    """未建立会话直接调用维持既有口径（MCP 会话未建立）。

    参数：无

    返回：
        None，通过断言验证上述行为，无返回值
    """
    session = McpSession(kind="http", url="http://localhost")
    with pytest.raises(ResearchSourceError, match="MCP 会话未建立"):
        await session.call_tool("get_flash")


async def test_full_lifecycle_via_async_with(monkeypatch) -> None:
    """完整生命周期：async with 进出后 runner 停止、底层会话收到关闭。

    参数：
        monkeypatch: pytest.MonkeyPatch，用于替换 _connect 跳过真实连接

    返回：
        None，通过断言验证上述行为，无返回值
    """
    fake = _FakeSession(lambda name, args: asyncio.sleep(0, result=_ok_text("ok")))

    async def fake_connect(self) -> None:
        """跳过真实连接，直接挂上 fake 底层会话。

        参数：无（self 由 monkeypatch 绑定）

        返回：
            None，就地写入 self._session
        """
        self._session = fake

    monkeypatch.setattr(McpSession, "_connect", fake_connect)
    session = McpSession(kind="http", url="http://localhost")
    async with session as s:
        assert await s.call_tool("get_flash") == "ok"
        runner = s._runner
        assert runner is not None and not runner.done()
    assert session._runner is None
    assert runner.done()
    assert fake.closed


async def test_connect_failure_stops_runner(monkeypatch) -> None:
    """连接失败：__aenter__ 抛 ResearchSourceError 且 runner 已停止（不留泄漏）。

    参数：
        monkeypatch: pytest.MonkeyPatch，用于替换 _connect 模拟连接失败

    返回：
        None，通过断言验证上述行为，无返回值
    """

    async def bad_connect(self) -> None:
        """模拟底层连接失败。

        参数：无（self 由 monkeypatch 绑定）

        返回：
            None，无正常返回

        异常：
            ResearchSourceError，模拟 MCP 连接失败
        """
        raise ResearchSourceError("MCP 连接失败（http）：refused")

    monkeypatch.setattr(McpSession, "_connect", bad_connect)
    session = McpSession(kind="http", url="http://localhost")
    runner = None
    with pytest.raises(ResearchSourceError, match="MCP 连接失败"):
        try:
            async with session:
                pass
        finally:
            runner = session._runner
    assert runner is None  # 失败路径 runner 已清理


def test_default_timeout_is_60_seconds() -> None:
    """连接/读取默认 60 秒超时（60 秒不返回即报错的配置口径）。

    参数：无

    返回：
        None，通过断言验证上述行为，无返回值
    """
    assert McpSession(kind="http", url="http://localhost")._timeout == 60.0


async def test_abandoned_runner_still_drains_close_work() -> None:
    """关闭序列被打断（关闭期二次取消）后，被抛弃的 runner 仍消费清理项与哨兵正常退出。

    回归（审查 M1）：runner 主循环原先每轮重读 self._queue——工作项抑制首次取消
    继续存活（anyio 病理形态），_shutdown 放弃路径已把实例属性置 None，runner
    下一轮即死于 AttributeError（未取回异常噪音），已投递的 _close_impl 再无人
    await（never awaited 警告）。修复后 runner 把队列抓进局部变量，不再回读实例属性。

    参数：无

    返回：
        None，断言实例属性已放弃、runner 仍干净退出且清理工作项实际执行
    """

    class _SuppressOnceSession(_FakeSession):
        """抑制首次取消继续存活的底层会话（模拟 anyio 内部吸收取消继续收尾的病理形态）。"""

        async def call_tool(self, name: str, args: dict) -> SimpleNamespace:
            """首次取消被吸收后继续挂起，第二次取消打断。

            参数：
                name: str，工具名
                args: dict，工具调用参数

            返回：
                SimpleNamespace，永不正常返回

            异常：
                asyncio.CancelledError：第二次取消时抛出（不再抑制）
            """
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                await asyncio.sleep(60)  # 抑制首次取消（anyio 内部收尾），第二次取消打断

    fake = _SuppressOnceSession(lambda name, args: None)
    session = McpSession(kind="http", url="http://localhost")
    _start_detached(session, fake)
    runner = session._runner

    call = asyncio.create_task(session.call_tool("get_flash"))
    await asyncio.sleep(0.02)  # 让挂起的工作项进入 runner
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call

    closing = asyncio.create_task(session.__aexit__(None, None, None))
    await asyncio.sleep(0.02)  # 让 _shutdown 进入 wait_for 等待 runner
    closing.cancel()  # 第一次：wait_for 取消 runner，工作项抑制取消继续存活
    await asyncio.sleep(0.02)  # 让抑制生效，closing 卡在等待 runner 死亡
    closing.cancel()  # 第二次：打断等待——_shutdown 走强取消 + 置 None + 重抛路径
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert session._runner is None  # 实例属性已放弃

    await asyncio.wait_for(runner, timeout=2)  # runner 不被 None 毒死：喝完清理项与哨兵
    assert fake.closed  # 清理工作项实际执行
