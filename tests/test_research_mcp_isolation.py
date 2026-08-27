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


async def test_connect_poisoned_by_anyio_cancel_scope(monkeypatch) -> None:
    """复现 2026-08-27 生产事故：initialize 期间 anyio 取消作用域开火且作用域未退出。

    取消作用域会每个事件循环周期向 runner 反复重投递 CancelledError（黏滞取消），
    曾落在 runner 主循环的 queue.get() 上杀死 runner，_shutdown 又误判为调用方
    取消重抛——整轮研报被打死。修复后：连接失败就地回卷作用域（止重投递源头），
    __aenter__ 只抛 ResearchSourceError，关闭序列干净完成。

    参数：
        monkeypatch: pytest.MonkeyPatch，替换 SDK 工厂注入中毒会话

    返回：
        None，通过断言验证上述行为，无返回值
    """
    import anyio

    class _PoisonedSession:
        """initialize 进入真实 anyio 任务组并取消其作用域（作用域保持打开，模拟 SDK 病理）。"""

        def __init__(self) -> None:
            """初始化中毒会话的表演状态。

            参数：无

            返回：
                None，就地写入实例属性
            """
            self._tg = None
            self.closed = False
            self.scope_exited = False

        async def __aenter__(self) -> "_PoisonedSession":
            """伪会话进入：直接返回自身。

            参数：无

            返回：
                _PoisonedSession，会话自身
            """
            return self

        async def initialize(self) -> None:
            """在 runner 任务上进入 anyio 任务组并取消其作用域，随后检查点抛 CancelledError。

            参数：无

            返回：
                None，无正常返回（检查点必抛 CancelledError，任务组故意保持打开）
            """
            self._tg = anyio.create_task_group()
            await self._tg.__aenter__()
            self._tg.cancel_scope.cancel()
            await anyio.sleep(0)  # 检查点：抛 CancelledError，取消的任务组仍开在 runner 任务上

        async def __aexit__(self, *exc_info: object) -> None:
            """伪会话关闭：退出任务组（回卷作用域），允许底层再抛取消（由调用方吞）。

            参数：
                exc_info: object，async with 退出时传入的异常信息，透传给任务组退出

            返回：
                None，就地写入 closed/scope_exited 标记
            """
            self.closed = True
            if self._tg is not None:
                try:
                    await self._tg.__aexit__(*exc_info)
                except BaseException:
                    pass
                self.scope_exited = True

    class _FakeCtx:
        """伪传输层上下文：记录 __aexit__ 是否被调用。"""

        def __init__(self) -> None:
            """初始化伪传输层上下文。

            参数：无

            返回：
                None，就地写入实例属性
            """
            self.closed = False

        async def __aenter__(self) -> tuple:
            """伪传输层进入：返回占位读写流。

            参数：无

            返回：
                tuple，占位的 (read, write) 二元组
            """
            return (object(), object())

        async def __aexit__(self, *exc_info: object) -> None:
            """伪传输层关闭：标记已关闭。

            参数：
                exc_info: object，async with 退出时传入的异常信息，本 fake 忽略

            返回：
                None，就地把 closed 置为 True
            """
            self.closed = True

    from src.research.providers import mcp_client as mcp_client_module

    poisoned = _PoisonedSession()
    ctx = _FakeCtx()
    monkeypatch.setattr(mcp_client_module.httpx2, "AsyncClient", lambda **kwargs: object())
    monkeypatch.setattr(mcp_client_module, "streamable_http_client", lambda url, http_client: ctx)
    monkeypatch.setattr(mcp_client_module, "ClientSession", lambda *a, **k: poisoned)

    session = McpSession(kind="http", url="http://localhost", token="t")
    with pytest.raises(ResearchSourceError, match="传输层内部取消"):
        async with session:
            pass
    assert session._runner is None  # 关闭序列完成，runner 已停止
    assert poisoned.closed and poisoned.scope_exited  # 连接失败就地回卷作用域（止重投递源头）
    assert ctx.closed


async def test_stray_cancel_on_idle_runner_does_not_break_shutdown() -> None:
    """黏滞取消重投递落在空闲 runner 的 queue.get() 上：runner 不死，关闭不误判。

    回归（2026-08-27 生产事故的另一半）：runner 主循环原先对 queue.get() 无防护，
    重投递直接杀死 runner；_shutdown 又把 runner 的死亡取消误判为调用方取消重抛。
    修复后 runner 对重投递免疫，会话继续可用，关闭后 runner 正常结束。

    参数：无

    返回：
        None，通过断言验证上述行为，无返回值
    """

    async def ok(name, args):
        """返回正常文本结果的伪工具行为。

        参数：
            name: str，工具名
            args: dict，工具调用参数

        返回：
            SimpleNamespace，带 content/is_error 字段的伪调用结果
        """
        return _ok_text("ok")

    session = McpSession(kind="http", url="http://localhost")
    _start_detached(session, _FakeSession(ok))
    runner = session._runner
    await asyncio.sleep(0.02)  # 让 runner 进入空闲等待（queue.get()）
    runner.cancel("simulated anyio re-delivery")  # 黏滞取消的重投递
    await asyncio.sleep(0.02)
    assert not runner.done()  # 重投递被主循环免疫，runner 存活
    assert await session.call_tool("get_flash") == "ok"  # 会话仍可用
    await session.__aexit__(None, None, None)
    assert runner.done() and not runner.cancelled()
    assert session._runner is None


async def test_poisoned_call_leaves_session_disposable() -> None:
    """工具调用期间 anyio 作用域开火且未退出：runner 在取消风暴中仍干净关闭。

    黏滞取消在调用后持续重投递（作用域未退出），runner 必须继续喝完清理项与
    哨兵；关闭后 runner 正常结束、不泄漏、关闭过程不再抛 CancelledError。

    参数：无

    返回：
        None，通过断言验证上述行为，无返回值
    """
    import anyio

    async def poison(name, args):
        """在 runner 任务上进入 anyio 任务组并取消其作用域（故意不退出）。

        参数：
            name: str，工具名
            args: dict，工具调用参数

        返回：
            SimpleNamespace，永不正常返回（检查点必抛 CancelledError）
        """
        tg = anyio.create_task_group()
        await tg.__aenter__()
        tg.cancel_scope.cancel()
        await anyio.sleep(0)

    session = McpSession(kind="http", url="http://localhost")
    _start_detached(session, _FakeSession(poison))
    with pytest.raises(ResearchSourceError, match="传输层内部取消"):
        await session.call_tool("get_flash")
    runner = session._runner
    await asyncio.wait_for(session.__aexit__(None, None, None), timeout=2)
    assert runner.done() and not runner.cancelled()
    assert session._runner is None


async def test_caller_cancel_during_shutdown_still_propagates() -> None:
    """防过度修复：关闭期间调用方真被取消，CancelledError 必须原样传播。

    _shutdown 按"调用方取消计数 > 0"区分真取消与 runner 死亡取消；本测试钉住
    真取消路径不被吞，且被抛弃的 runner 喝完哨兵正常退出。

    参数：无

    返回：
        None，通过断言验证上述行为，无返回值
    """

    class _HangCloseSession(_FakeSession):
        """关闭时挂起的伪底层会话（拖住 _shutdown 的等待窗口）。"""

        async def __aexit__(self, *exc_info: object) -> None:
            """挂起 60 秒的伪关闭（等待测试取消）。

            参数：
                exc_info: object，async with 退出时传入的异常信息，本 fake 忽略

            返回：
                None，无正常返回（测试在挂起期间取消）
            """
            await asyncio.sleep(60)

    fake = _HangCloseSession(lambda name, args: None)
    session = McpSession(kind="http", url="http://localhost")
    _start_detached(session, fake)
    runner = session._runner
    closing = asyncio.create_task(session.__aexit__(None, None, None))
    await asyncio.sleep(0.02)  # 让 _shutdown 进入 wait_for（runner 卡在关闭工作项）
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert session._runner is None  # 实例属性已放弃
    await asyncio.wait_for(runner, timeout=2)  # runner 喝完哨兵正常退出，不泄漏
