"""FastAPI 监控后端入口：create_app(deps) 组装路由、CORS、WebSocket 与静态托管。

server 与 agent 同进程 asyncio 运行；web/dist 存在时挂载到 /（不存在不报错），
前后端单端口运行（默认 17577，见 ServerConfig）。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from src.server.deps import ServerDeps
from src.server.routes_config import create_config_router
from src.server.routes_review import create_review_router
from src.server.routes_status import create_status_router
from src.server.routes_trading import create_trading_router
from src.server.ws import ConnectionManager, pump_events, register_ws_route

# 前端 Vite dev server 来源
_DEV_ORIGINS = ["http://localhost:17576"]


def create_app(deps: ServerDeps) -> FastAPI:
    manager = ConnectionManager()

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # 注入了事件队列时启动后台广播任务，关闭时取消
        task: asyncio.Task | None = None
        if deps.event_queue is not None:
            task = asyncio.create_task(pump_events(manager, deps.event_queue))
        yield
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    app = FastAPI(title="LLM 交易监控", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_DEV_ORIGINS,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(create_status_router(deps))
    app.include_router(create_config_router(deps))
    app.include_router(create_trading_router(deps))
    app.include_router(create_review_router(deps))
    register_ws_route(app, manager)
    app.state.ws_manager = manager  # 供主程序直接推送事件
    if deps.web_dist.is_dir():
        app.mount("/", StaticFiles(directory=deps.web_dist, html=True), name="web")
    return app
