"""MCP 会话封装：HTTP（金十）与 stdio（律动）统一复用连接。

mcp 2.0 SDK：streamable_http_client / stdio_client + ClientSession；
连接或调用失败抛 ResearchSourceError（工具层转中文哨兵，不中断研报轮）。
"""

from __future__ import annotations

import os
import shlex
import sys
from collections.abc import AsyncIterator
from typing import Literal

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


class McpSession:
    """一个 MCP 会话：async with 块内可多次 call_tool，退出时释放连接/子进程。

    kind='http'：金十（Bearer token）；kind='stdio'：律动（npx 子进程 + API key）。
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

    async def __aenter__(self) -> "McpSession":
        """按配置建立 MCP 连接并完成初始化握手，返回就绪会话；失败先清理资源再抛错。

        参数：无

        返回：
            McpSession：已建立连接的会话自身，供 async with 语句绑定使用

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
            return self
        except Exception as exc:
            await self.__aexit__(None, None, None)
            raise ResearchSourceError(f"MCP 连接失败（{self._kind}）：{exc}") from exc

    async def __aexit__(self, *exc_info: object) -> None:
        """释放 MCP 会话与底层连接/子进程资源；清理中的异常被吞掉，保证退出不再抛错。

        参数：
            exc_info: object，async with 退出时传入的异常信息（异常类型、值、追溯），
                仅原样透传给底层会话与传输层的关闭逻辑

        返回：
            None，就地释放资源（会话与连接上下文置为 None）
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
        try:
            result = await self._session.call_tool(name, args or {})
            text = "".join(c.text or "" for c in result.content)
            if result.is_error:  # mcp 2.0 字段名（旧版为 isError）
                raise ResearchSourceError(f"MCP 工具 {name} 报错：{text[:200]}")
            return text
        except ResearchSourceError:
            raise
        except Exception as exc:
            raise ResearchSourceError(f"MCP 工具 {name} 调用失败：{exc}") from exc

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
        tools = await self._session.list_tools()
        return [t.name for t in tools.tools]
