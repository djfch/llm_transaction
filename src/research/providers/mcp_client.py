"""MCP 会话封装：HTTP（金十）与 stdio（律动）统一复用连接。

mcp 2.0 SDK：streamable_http_client / stdio_client + ClientSession；
连接或调用失败抛 ResearchSourceError（工具层转中文哨兵，不中断研报轮）。

runner 隔离：anyio 任务组在网络抖动/超时时会把内部取消以 CancelledError
（非 Exception 子类）漏进调用协程，并在后续每次 await 反复再抛，曾打穿研报
收尾并杀死调度循环。McpSession 因此把连接/调用/关闭全部投递给一个专属
runner 任务执行——anyio 作用域整个活在 runner 内，泄漏取消在 runner 边界
就地转成 ResearchSourceError；调用方真被外部取消（shutdown）时其 await 点
由事件循环正常抛 CancelledError，原样传播，取消语义不变。
"""

from __future__ import annotations

import asyncio
import os
import shlex
import sys
from collections.abc import AsyncIterator, Awaitable
from typing import Literal, TypeVar

from src.research.providers.base import ResearchSourceError

try:
    import httpx2  # mcp 2.0 的内部 HTTP 客户端（间接依赖，直接使用）
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.client.streamable_http import streamable_http_client
except ImportError as exc:  # 依赖缺失：给可读错误，不静默降级
    raise ImportError(f"研报 MCP 依赖缺失：{exc}") from exc

# 敏感环境变量前缀/键名：不透传给第三方 npx 子进程（防交易密钥泄露，M8）
_SENSITIVE_ENV_PREFIXES = (
    "GATE_API_",
    "JIN10_MCP_",
    "BLOCKBEATS_",
    "FRED_",
    "LLM_KEY_",
    "ANTHROPIC_",
    "OPENAI_",
    "TELEGRAM_",
)
_SENSITIVE_ENV_KEYS = frozenset({"API_KEY", "API_SECRET", "TOKEN", "PASSWORD", "SECRET", "AUTH"})

_T = TypeVar("_T")


def _minimal_env() -> dict[str, str]:
    """复制系统环境但剔除敏感项（第三方 MCP 子进程只需 PATH 等运行环境）。

    参数：无

    返回：
        dict[str, str]，保留运行必需项且剔除敏感项的子进程环境
    """
    return {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(_SENSITIVE_ENV_PREFIXES)
        and not any(marker in k.upper() for marker in _SENSITIVE_ENV_KEYS)
    }


def _stdio_command(cmd: str) -> tuple[str, list[str]]:
    """把 'npx -y blockbeats-mcp' 命令串拆成 (command, args)。

    平台分支（M1 修复）：Windows 上 npx 是 .cmd 批处理，CreateProcess 不能直接
    执行，须经 cmd /c 包装；POSIX（Linux 部署机）无 cmd，shlex 拆分后直接 exec。
    Windows 分支用普通 split——shlex 按 POSIX 规则吃反斜杠，会拆坏
    'C:\\tools\\xx.cmd' 这类路径型自定义命令（复审 #3 修复）。

    参数：
        cmd: str，待拆分的 stdio MCP 命令串

    返回：
        tuple[str, list[str]]，可执行命令与参数列表

    异常：
        ResearchSourceError，拆分后的 MCP 命令为空时抛出

    """
    if not cmd.strip():
        raise ResearchSourceError("stdio MCP 命令为空")
    if sys.platform == "win32":
        parts = cmd.split()
        return "cmd", ["/c", *parts]
    parts = shlex.split(cmd)
    return parts[0], parts[1:]


def _fmt_exc(exc: BaseException) -> str:
    """格式化底层异常供错误信息使用：异常组取首个子异常，其余取 str。

    参数：
        exc: BaseException，底层抛出的异常

    返回：
        str，可读的单行异常描述（异常组避免只显示 'N sub-exception' 外壳）
    """
    if isinstance(exc, BaseExceptionGroup):
        return str(exc.exceptions[0])
    return str(exc)


def _consume_result(fut: asyncio.Future) -> None:
    """消费调用方被取消后才完成的工作项结果，防 'exception was never retrieved' 警告。

    参数：
        fut: asyncio.Future，调用方取消时无人再 await 的工作项结果 Future

    返回：
        None，就地取回异常（已取消的 Future 无异常可取，直接跳过）
    """
    if not fut.cancelled():
        fut.exception()


class McpSession:
    """一个 MCP 会话：async with 块内可多次 call_tool，退出时释放连接/子进程。

    kind='http'：金十（Bearer token）；kind='stdio'：律动（npx 子进程 + API key）。
    所有底层操作投递给专属 runner 任务执行：anyio 泄漏的 CancelledError/异常组
    在 runner 边界转成 ResearchSourceError，不再漏进调用方协程。
    """

    def __init__(
        self,
        *,
        kind: Literal["http", "stdio"],
        url: str = "",
        token: str = "",
        cmd: str = "",
        env_key: str = "",
        timeout: float = 60.0,
    ) -> None:
        """保存一个 MCP 会话的连接配置并校验必填项，此时尚不建立连接。

        参数：
            kind: Literal["http", "stdio"]，会话类型：http 为金十（Bearer token 直连），
                stdio 为律动（npx 子进程 + API key）
            url: str，HTTP 模式的 MCP 服务地址；kind='http' 时必填
            token: str，HTTP 模式的 Bearer 鉴权令牌；可为空串表示不鉴权
            cmd: str，stdio 模式的启动命令串（如 'npx -y blockbeats-mcp'）；
                kind='stdio' 时必填
            env_key: str，stdio 模式需额外透传给子进程的环境变量名（如律动 API key）；
                为空串则不透传任何敏感变量
            timeout: float，连接与读取超时秒数；省略时默认 60 秒

        返回：
            None，就地写入实例属性（连接在 async with 进入时才建立）

        异常：
            ResearchSourceError：kind='http' 但缺少 url，或 kind='stdio' 但缺少 cmd 时抛出
        """
        if kind == "http" and not url:
            raise ResearchSourceError("HTTP MCP 缺少 url")
        if kind == "stdio" and not cmd:
            raise ResearchSourceError("stdio MCP 缺少命令")
        self._kind = kind
        self._url = url
        self._token = token
        self._cmd = cmd
        self._env_key = env_key
        self._timeout = timeout
        self._session: ClientSession | None = None
        self._ctx: AsyncIterator | None = None
        self._queue: asyncio.Queue | None = None
        self._runner: asyncio.Task | None = None

    async def __aenter__(self) -> "McpSession":
        """启动 runner 任务并在其内建立 MCP 连接，返回就绪会话；失败先停 runner 再抛错。

        参数：无

        返回：
            McpSession：已建立连接的会话自身，供 async with 语句绑定使用

        异常：
            ResearchSourceError：连接、会话建立或初始化任一步骤失败时抛出，
                原始异常保留在 __cause__ 中
        """
        self._queue = asyncio.Queue()
        self._runner = asyncio.create_task(self._runner_loop(), name=f"mcp-runner-{self._kind}")
        try:
            await self._dispatch(self._connect())
        except BaseException:
            await self._shutdown((None, None, None))
            raise
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        """经 runner 释放 MCP 会话与底层连接/子进程并停止 runner；清理中的普通异常不再外抛。

        参数：
            exc_info: object，async with 退出时传入的异常信息（异常类型、值、追溯），
                仅原样透传给底层会话与传输层的关闭逻辑

        返回：
            None，就地释放资源（会话、连接上下文与 runner 置为 None）

        异常：
            asyncio.CancelledError：调用方在关闭期间被外部取消时原样抛出
        """
        await self._shutdown(exc_info)

    async def call_tool(self, name: str, args: dict | None = None) -> str:
        """调用一次 MCP 工具，返回拼接后的文本；失败抛 ResearchSourceError。

        参数：
            name: str，工具名或参数名
            args: dict | None，工具调用参数

        返回：
            str，调用一次 MCP 工具，返回拼接后的文本；失败抛 ResearchSourceError

        异常：
            ResearchSourceError，会话未建立、工具返回错误标记或调用过程失败时抛出
        """
        if self._session is None:
            raise ResearchSourceError("MCP 会话未建立")
        return await self._dispatch(self._call_impl(name, args))

    async def list_tools(self) -> list[str]:
        """列出可用工具名（连通性自检用）。

        参数：无

        返回：
            list[str]，列出可用工具名（连通性自检用）

        异常：
            ResearchSourceError，MCP 会话尚未建立时抛出
        """
        if self._session is None:
            raise ResearchSourceError("MCP 会话未建立")
        return await self._dispatch(self._list_tools_impl())

    async def _runner_loop(self) -> None:
        """runner 主循环：顺序执行投递的工作项，结果/异常经 Future 回传调用方。

        anyio 任务组整个活在本任务内（进出同任务，满足 anyio 约束）；工作项逃逸的
        任何 BaseException（含泄漏取消、异常组）就地转成 ResearchSourceError，
        runner 自身不因此死亡，仅由停止哨兵结束。

        参数：无

        返回：
            None，收到停止哨兵后结束（工作项结果经各自 Future 回传）
        """
        queue = self._queue  # 抓局部引用：_shutdown 放弃路径（关闭期间被取消/超时）
        # 会把实例属性置 None——runner 若抑制取消继续收尾，回读属性会拿到 None
        # 死于 AttributeError，已投递的清理项也再无人 await
        while True:
            work, fut = await queue.get()
            if work is None:
                return
            try:
                result = await work
            except BaseException as exc:
                if not fut.done():
                    fut.set_exception(self._contained_error(exc))
            else:
                if not fut.done():
                    fut.set_result(result)

    async def _dispatch(self, work: Awaitable[_T]) -> _T:
        """把协程工作项投递给 runner 执行并等待结果；调用方真被取消时原样传播。

        参数：
            work: Awaitable[_T]，待 runner 执行的协程工作项

        返回：
            _T，工作项的执行结果

        异常：
            ResearchSourceError：工作项失败（含 runner 边界转换的泄漏取消/异常组）时抛出；
            asyncio.CancelledError：调用方被外部取消（shutdown）时原样抛出
        """
        fut: asyncio.Future[_T] = asyncio.get_running_loop().create_future()
        self._queue.put_nowait((work, fut))
        try:
            return await fut
        except asyncio.CancelledError:
            fut.add_done_callback(_consume_result)
            raise

    def _contained_error(self, exc: BaseException) -> ResearchSourceError:
        """把逃逸出工作项的异常统一转成 ResearchSourceError（泄漏取消不再越界）。

        参数：
            exc: BaseException，runner 内工作项逃逸的异常

        返回：
            ResearchSourceError：已是 ResearchSourceError 的原样返回；CancelledError
                按传输层内部取消转写；异常组取首个子异常；其余按调用失败转写
        """
        if isinstance(exc, ResearchSourceError):
            return exc
        if isinstance(exc, asyncio.CancelledError):
            return ResearchSourceError(f"MCP 传输层内部取消（{self._kind}）：已按数据源失败隔离")
        return ResearchSourceError(f"MCP 调用失败（{self._kind}）：{_fmt_exc(exc)}")

    async def _connect(self) -> None:
        """在 runner 任务内按配置建立 MCP 连接并完成初始化握手（原 __aenter__ 连接逻辑）。

        参数：无

        返回：
            None，就地写入 _session/_ctx（连接关闭逻辑由 _close_impl 在同任务内执行）

        异常：
            ResearchSourceError：连接、会话建立或初始化任一步骤失败时抛出，
                原始异常保留在 __cause__ 中
        """
        try:
            if self._kind == "http":
                http = httpx2.AsyncClient(
                    headers={"Authorization": f"Bearer {self._token}"}, timeout=self._timeout
                )
                self._ctx = streamable_http_client(self._url, http_client=http)
            else:
                env = _minimal_env()
                if self._env_key:
                    env[self._env_key] = os.environ.get(self._env_key, "")
                command, args = _stdio_command(self._cmd)
                params = StdioServerParameters(command=command, args=args, env=env)
                self._ctx = stdio_client(params)
            read, write = await self._ctx.__aenter__()
            self._session = ClientSession(read, write, read_timeout_seconds=self._timeout)
            await self._session.__aenter__()
            await self._session.initialize()
        except Exception as exc:
            raise ResearchSourceError(f"MCP 连接失败（{self._kind}）：{_fmt_exc(exc)}") from exc

    async def _call_impl(self, name: str, args: dict | None) -> str:
        """在 runner 任务内执行一次 MCP 工具调用（原 call_tool 调用逻辑）。

        参数：
            name: str，工具名或参数名
            args: dict | None，工具调用参数

        返回：
            str，调用一次 MCP 工具，返回拼接后的文本

        异常：
            ResearchSourceError，工具返回错误标记或调用过程失败时抛出
        """
        try:
            result = await self._session.call_tool(name, args or {})
            text = "".join(c.text or "" for c in result.content)
            if result.is_error:  # mcp 2.0 字段名（旧版为 isError）
                raise ResearchSourceError(f"MCP 工具 {name} 报错：{text[:200]}")
            return text
        except ResearchSourceError:
            raise
        except Exception as exc:
            raise ResearchSourceError(f"MCP 工具 {name} 调用失败：{_fmt_exc(exc)}") from exc

    async def _list_tools_impl(self) -> list[str]:
        """在 runner 任务内查询底层会话可用工具名（原 list_tools 查询逻辑）。

        参数：无

        返回：
            list[str]，底层会话报告的工具名列表
        """
        tools = await self._session.list_tools()
        return [t.name for t in tools.tools]

    async def _close_impl(self, exc_info: tuple) -> None:
        """在 runner 任务内释放会话与底层连接/子进程（原 __aexit__ 清理逻辑）。

        参数：
            exc_info: tuple，调用方 async with 退出时的异常信息三元组，原样透传给
                底层会话与传输层的关闭逻辑

        返回：
            None，就地释放资源（会话与连接上下文置为 None）；清理中的异常被吞掉
        """
        if self._session is not None:
            try:
                await self._session.__aexit__(*exc_info)
            except Exception:
                pass
            self._session = None
        if self._ctx is not None:
            try:
                await self._ctx.__aexit__(*exc_info)
            except Exception:
                pass
            self._ctx = None

    async def _shutdown(self, exc_info: tuple) -> None:
        """投递清理工作项与停止哨兵后等待 runner 退出；超时强制取消 runner。

        参数：
            exc_info: tuple，调用方 async with 退出时的异常信息三元组

        返回：
            None，就地停止 runner 并将其与队列置为 None；正常路径下清理工作项
                已在 runner 内执行完毕（哨兵排在清理之后）；超时路径取消 runner
                并等待其终止——若 runner 内工作项抑制取消，终止时点由底层操作
                自身超时兜底（软上限，非严格 5 秒硬截止）

        异常：
            asyncio.CancelledError：调用方在关闭期间被外部取消时原样抛出
                （runner 已被强制取消；runner 隔离后此处取消只可能来自调用方）
        """
        if self._runner is None:
            return
        close_fut: asyncio.Future = asyncio.get_running_loop().create_future()
        close_fut.add_done_callback(_consume_result)
        self._queue.put_nowait((self._close_impl(exc_info), close_fut))
        self._queue.put_nowait((None, None))
        try:
            await asyncio.wait_for(self._runner, timeout=5)
        except BaseException as exc:
            self._runner.cancel()
            self._runner = None
            self._queue = None
            if isinstance(exc, asyncio.CancelledError):
                raise
            return
        self._runner = None
        self._queue = None
