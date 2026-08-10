"""WebSocket /ws：连接管理器与事件广播。

当前广播事件（均由主程序经注入的 event_queue 放入）：
- {"type": "round_start", ...} 决策轮开始、{"type": "round", ...} 决策轮结束
- {"type": "ticker", "data": {"contract", "last"}} 行情推送（bootstrap 按合约节流）
- {"type": "plan_updated"} 交易计划变更（执行 agent 工具轮中即推，无 payload，只作失效信号）
- {"type": "strategy_updated"} 策略书变更（复盘修订/手动保存/回滚，同为失效信号）
- {"type": "indicator_config_updated"} 指标短名单变更（复盘修订/人工修订/回滚，同为失效信号）
- {"type": "review_round_start", "data": {"round_id"}} 复盘审计轮开始（begin_round 之后即推）、
  {"type": "review_round", "data": {"round_id", "ok"}} 复盘结束（成功/失败均发）；
  两者均为失效信号，复盘轮数据走 REST /api/review/live（不复用 round_start/round，避免误触发交易面板刷新）
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
        """初始化连接管理器，创建空的活跃连接集合。

        参数：无

        返回：None，初始化实例内部状态（空连接集合）
        """
        self._connections: set[WebSocket] = set()

    @property
    def count(self) -> int:
        """读取当前活跃连接数。

        参数：无

        返回：
            int：当前保持中的 WebSocket 连接数量
        """
        return len(self._connections)

    async def connect(self, ws: WebSocket) -> None:
        """接受一个新的 WebSocket 连接并纳入管理，随后回发 hello 握手消息。

        参数：
            ws: WebSocket，客户端发起的 WebSocket 连接

        返回：None，把连接加入活跃集合并向客户端发送 {"type": "hello"}
        """
        await ws.accept()
        self._connections.add(ws)
        await ws.send_json({"type": "hello"})

    def disconnect(self, ws: WebSocket) -> None:
        """把连接从活跃集合中移除；连接不在集合中时不报错。

        参数：
            ws: WebSocket，要移除的 WebSocket 连接

        返回：None，就地修改活跃连接集合
        """
        self._connections.discard(ws)

    async def broadcast(self, payload: dict[str, Any]) -> None:
        """向全部连接广播任意 JSON；发送失败的连接视为已断开并移除。

        参数：
            payload: dict[str, Any]，待广播、保存或转换的数据载荷

        返回：
            None：向全部连接广播任意 JSON；发送失败的连接视为已断开并移除
        """
        dead: list[WebSocket] = []
        for ws in list(self._connections):
            try:
                await ws.send_json(payload)
            except Exception:  # noqa: BLE001 连接中断原因多样，统一按断开处理
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


async def pump_events(manager: ConnectionManager, queue: asyncio.Queue) -> None:
    """后台任务：持续从事件队列取事件并广播，由 lifespan 取消而结束。

    参数：
        manager: ConnectionManager，WebSocket 连接管理器
        queue: asyncio.Queue，待广播事件队列

    返回：
        None：后台任务：持续从事件队列取事件并广播，由 lifespan 取消而结束
    """
    while True:
        payload = await queue.get()
        await manager.broadcast(payload)


def register_ws_route(app: FastAPI, manager: ConnectionManager) -> None:
    """注册 /ws 路由：保持连接，客户端上行消息忽略（推送是单向的）。

    参数：
        app: FastAPI，FastAPI 应用实例
        manager: ConnectionManager，WebSocket 连接管理器

    返回：
        None：注册 /ws 路由：保持连接，客户端上行消息忽略（推送是单向的）
    """

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        """/ws 路由的处理入口，把连接交给 ws_connection 管理整个生命周期。

        参数：
            ws: WebSocket，客户端建立的 WebSocket 连接

        返回：None，保持连接直到客户端断开或接收出错后由管理器移除
        """
        await ws_connection(ws, manager)


async def ws_connection(ws: WebSocket, manager: ConnectionManager) -> None:
    """单个 /ws 连接的生命周期：保持接收直到断开。

    finally 保证任何退出路径（正常断开、接收侧抛错）都从 manager 移除连接，
    避免异常导致连接残留、后续广播持续向死连接发送。

    参数：
        ws: WebSocket，当前 WebSocket 连接
        manager: ConnectionManager，WebSocket 连接管理器

    返回：
        None：单个 /ws 连接的生命周期：保持接收直到断开
    """
    await manager.connect(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(ws)
