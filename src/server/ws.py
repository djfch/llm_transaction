"""WebSocket /ws：连接管理器与事件广播。

当前广播事件（均由主程序经注入的 event_queue 放入）：
- {"type": "round_start", ...} 决策轮开始、{"type": "round", ...} 决策轮结束
- {"type": "ticker", "data": {"contract", "last"}} 行情推送（bootstrap 按合约节流）
- {"type": "plan_updated"} 交易计划变更（执行 agent 工具轮中即推，无 payload，只作失效信号）
- {"type": "strategy_updated"} 策略书变更（复盘修订/手动保存/回滚，同为失效信号）
- {"type": "indicator_config_updated"} 指标短名单变更（复盘修订/人工修订/回滚，同为失效信号）
- {"type": "trades_updated", "data": {"contracts", "count"}} 成交落库成功
  （paper drain/手动平仓与 testnet/live fill_sync 对账落库统一发射；只作失效信号，成交数据仍走 REST）
当前主程序不生产 trade/position 事件；event_queue 接受任意 JSON 字典，pump_events
会原样广播给全部连接。连接建立时先回一条 hello 握手消息。
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect


class ConnectionManager:
    """管理 /ws 的全部活跃连接。"""

    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()

    @property
    def count(self) -> int:
        return len(self._connections)

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._connections.add(ws)
        await ws.send_json({"type": "hello"})

    def disconnect(self, ws: WebSocket) -> None:
        self._connections.discard(ws)

    async def broadcast(self, payload: dict[str, Any]) -> None:
        """向全部连接广播任意 JSON；发送失败的连接视为已断开并移除。"""
        dead: list[WebSocket] = []
        for ws in list(self._connections):
            try:
                await ws.send_json(payload)
            except Exception:  # noqa: BLE001 连接中断原因多样，统一按断开处理
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


async def pump_events(manager: ConnectionManager, queue: asyncio.Queue) -> None:
    """后台任务：持续从事件队列取事件并广播，由 lifespan 取消而结束。"""
    while True:
        payload = await queue.get()
        await manager.broadcast(payload)


def register_ws_route(app: FastAPI, manager: ConnectionManager) -> None:
    """注册 /ws 路由：保持连接，客户端上行消息忽略（推送是单向的）。"""

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        await ws_connection(ws, manager)


async def ws_connection(ws: WebSocket, manager: ConnectionManager) -> None:
    """单个 /ws 连接的生命周期：保持接收直到断开。

    finally 保证任何退出路径（正常断开、接收侧抛错）都从 manager 移除连接，
    避免异常导致连接残留、后续广播持续向死连接发送。
    """
    await manager.connect(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(ws)
