"""FastAPI 监控后端入口：create_app(deps) 组装路由、CORS、WebSocket 与静态托管。

server 与 agent 同进程 asyncio 运行；web/dist 存在时挂载到 /（不存在不报错），
前后端单端口运行（默认 17577，见 ServerConfig）。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from src.server.deps import ServerDeps
from src.server.routes_config import create_config_router
from src.server.routes_credentials import create_credentials_router
from src.server.routes_indicators import create_indicators_router
from src.server.routes_plans import create_plans_router
from src.server.routes_research import create_research_router
from src.server.routes_review import create_review_router
from src.server.routes_status import create_status_router
from src.server.routes_trading import create_trading_router
from src.server.ws import ConnectionManager, pump_events, register_ws_route

# 前端 Vite dev server 来源
_DEV_ORIGINS = ["http://localhost:17576"]


def _safe_validation_errors(exc: RequestValidationError) -> list[dict[str, Any]]:
    """剔除每个 error 的 input 键（密钥铁规：pydantic 默认把请求原值放进 detail[].input，
    api_key 等敏感字段会随 422 响应明文回显）；其余字段（type/loc/msg/url/ctx）保留。
    ctx 可能含异常对象（自定义校验器的 ValueError），经 custom_encoder 兜底序列化。

    参数：
        exc: RequestValidationError，捕获到的原始异常

    返回：
        list[dict[str, Any]]：剔除每个 error 的 input 键（密钥铁规：pydantic 默认把请求原值放进 detail[].input，
    """
    return [
        jsonable_encoder(
            {k: v for k, v in err.items() if k != "input"},
            custom_encoder={ValueError: str},
        )
        for err in exc.errors()
    ]


def create_app(deps: ServerDeps) -> FastAPI:
    """组装 FastAPI 监控应用：注册全局 422 处理器、CORS、各业务路由、WebSocket 与前端静态托管。

    参数：
        deps: ServerDeps，服务层全部运行时依赖（仓库、网关、事件队列、web 静态目录等），
            由主程序接线注入

    返回：
        FastAPI：装配完成的监控应用；web/dist 目录存在时挂载到 / 提供前端页面，
        不存在则仅提供 API 与 WebSocket
    """
    manager = ConnectionManager()

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        """管理应用生命周期：注入了事件队列时启动后台广播任务，关闭时取消该任务。

        广播任务持续把事件队列中的事件推送给所有 WebSocket 客户端。

        参数：
            _app: FastAPI，FastAPI 传入的应用实例（lifespan 协议要求，函数体内未使用）

        返回：
            AsyncIterator[None]：生命周期异步迭代器，yield 前完成启动逻辑，
            yield 后执行关闭清理
        """
        task: asyncio.Task | None = None
        if deps.event_queue is not None:
            task = asyncio.create_task(pump_events(manager, deps.event_queue))
        yield
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    app = FastAPI(title="LLM 交易监控", lifespan=lifespan)

    @app.exception_handler(RequestValidationError)
    async def _request_validation_handler(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """全局 422 处理器：保持 422 状态与 detail 数组结构，剥离 input 明文（密钥铁规）。

        参数：
            _request: Request，未使用的 FastAPI 请求对象
            exc: RequestValidationError，捕获到的原始异常

        返回：
            JSONResponse：全局 422 处理器：保持 422 状态与 detail 数组结构，剥离 input 明文（密钥铁规）
        """
        return JSONResponse(status_code=422, content={"detail": _safe_validation_errors(exc)})

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_DEV_ORIGINS,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(create_status_router(deps))
    app.include_router(create_config_router(deps))
    app.include_router(create_credentials_router(deps))
    app.include_router(create_trading_router(deps))
    app.include_router(create_review_router(deps))
    app.include_router(create_research_router(deps))
    app.include_router(create_indicators_router(deps))
    app.include_router(create_plans_router(deps))
    register_ws_route(app, manager)
    app.state.ws_manager = manager  # 供主程序直接推送事件
    if deps.web_dist.is_dir():
        app.mount("/", StaticFiles(directory=deps.web_dist, html=True), name="web")
    return app
