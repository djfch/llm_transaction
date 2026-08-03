"""手动实测脚本：Gate 私有 WS（成交/自动订单/强平）+ 平仓盈亏接口字段实测（沙盒资金）。

用途（PR3 前置，AGENTS.md Gate 规范：字段以实测为准，禁止猜测）：
1. 订阅 futures.usertrades / futures.autoorders / futures.liquidates（payload ["!all"]），
   打印每条原始 payload——确认成交推送字段全集、autoorders 触发后是否含成交订单 id、
   liquidates 是否给强平订单 id（决定强平识别走集合还是文本）
2. 平仓成交后每 1 秒轮询 position_close，确认记录出现延迟与粒度（每笔一条还是聚合）
流程：市价开最小多仓 → 挂"立即触发"的止盈单（触发价略低于标记价）→ 等推送 → 轮询 position_close。
安全约束：强制断言 testnet host；finally 兜底平掉残留持仓。不进测试套件。
用法：uv run python scripts/verify_private_feed.py
"""

import asyncio
import json
import os
import sys
import time
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

from gate_ws import Configuration, Connection, WebSocketResponse  # noqa: E402
from gate_ws.futures import (  # noqa: E402
    FuturesAutoOrdersChannel,
    FuturesLiquidatesChannel,
    FuturesUserTradesChannel,
)

from src.config import Settings, load_settings  # noqa: E402
from src.gateway.base import OrderRequest, TpslOrder  # noqa: E402
from src.gateway.gate_rest import GateRestGateway  # noqa: E402

CONTRACT = "BTC_USDT"


def _ts() -> str:
    return time.strftime("%H:%M:%S")


def _print_payload(channel: str, response: WebSocketResponse) -> None:
    if response.error:
        print(f"[{_ts()}] {channel} ACK/ERROR: {response.error}")
        return
    if response.event != "update":
        print(f"[{_ts()}] {channel} ACK: event={response.event}")
        return
    for raw in response.result or []:
        keys = sorted(raw.keys()) if isinstance(raw, dict) else type(raw)
        print(f"[{_ts()}] {channel} UPDATE keys={keys}\n  {json.dumps(raw, ensure_ascii=False)}")


def _build() -> tuple[Settings, GateRestGateway, Connection]:
    """加载配置与密钥，构建 testnet 网关与私有 WS 连接；host 必须含 testnet。"""
    load_dotenv()
    settings = load_settings()
    if "testnet" not in settings.gate.testnet_host:
        sys.exit(f"拒绝执行：testnet_host 不含 testnet -> {settings.gate.testnet_host}")
    key, secret = os.environ.get("GATE_API_KEY", ""), os.environ.get("GATE_API_SECRET", "")
    if not key or not secret:
        sys.exit("拒绝执行：.env 未配置 GATE_API_KEY/GATE_API_SECRET")
    gw = GateRestGateway(settings.gate, api_key=key, api_secret=secret, testnet=True)
    conn = Connection(
        Configuration(
            app="futures",
            settle=settings.gate.settle,
            test_net=True,
            # SDK 内置 testnet WS 地址已失效，用配置覆盖
            host=settings.gate.testnet_ws_host,
            api_key=key,
            api_secret=secret,
        )
    )
    return settings, gw, conn


def _subscribe_private(conn: Connection) -> None:
    """订阅三条私有频道（payload ["!all"]），原始 payload 全量打印。"""
    FuturesUserTradesChannel(conn, lambda c, r: _print_payload("usertrades", r)).subscribe(["!all"])
    FuturesAutoOrdersChannel(conn, lambda c, r: _print_payload("autoorders", r)).subscribe(["!all"])
    FuturesLiquidatesChannel(conn, lambda c, r: _print_payload("liquidates", r)).subscribe(["!all"])


async def _open_then_trigger_tpsl(gw: GateRestGateway) -> float | None:
    """市价开最小多仓 → 挂略高于标记价的止盈等触发；超时未触发则市价平仓兜底
    （autoorders 触发字段可能观测不到，但 usertrades/position_close 仍可实测）。
    Gate 校验：多仓止盈触发价必须 > 最新价（禁止"立即触发"挂法），返回平仓时刻。"""
    contract = gw.get_contract(CONTRACT)
    size = int(contract.order_size_min)
    mark = contract.mark_price
    print(f"[{_ts()}] {CONTRACT} mark={mark} min_size={size}，市价开多…")
    result = gw.place_order(OrderRequest(contract=CONTRACT, size=size))
    print(f"[{_ts()}] 开仓回报：id={result.id} status={result.status} text={result.text}")
    await asyncio.sleep(4)  # 等 usertrades 开仓推送
    trigger = (mark * Decimal("1.001")).quantize(contract.order_price_round)
    tpsl = gw.create_tpsl_order(
        TpslOrder(id="", contract=CONTRACT, direction=1, kind="take_profit", trigger_price=trigger)
    )
    print(f"[{_ts()}] 已挂止盈（触发价 {trigger} > 标记价，等价格上触）：id={tpsl.id}")
    for _ in range(40):  # 等止盈触发 + 平仓成交推送
        await asyncio.sleep(1)
        holding = [p for p in gw.list_positions() if p.contract == CONTRACT and p.size != 0]
        if not holding:
            print(f"[{_ts()}] 持仓已平（止盈触发）")
            return time.time()
    print(f"[{_ts()}] 40 秒未触发止盈，改市价平仓兜底（autoorders 触发字段本次观测不到）")
    gw.place_order(OrderRequest(contract=CONTRACT, close=True))
    await asyncio.sleep(3)  # 等平仓成交推送
    return time.time()


def _print_my_trades(gw: GateRestGateway) -> None:
    """打印个人成交历史原始字段（gate_api 直查，字段全集）。"""
    print(f"[{_ts()}] —— my_trades 原始字段 ——")
    for t in gw._api.get_my_trades(gw._settle, contract=CONTRACT, limit=5):
        print(f"  {t.to_dict()}")


async def _poll_position_close(gw: GateRestGateway, close_ts: float | None) -> None:
    """平仓后每 1 秒轮询 position_close：实测记录出现延迟与粒度；并验证 _from/to 窗口过滤。"""
    print(f"[{_ts()}] —— position_close 延迟与粒度实测 ——")
    for i in range(15):
        rows = gw._api.list_position_close(gw._settle, contract=CONTRACT, limit=5)
        if rows:
            delay = f"{time.time() - close_ts:.0f}s" if close_ts else "未知"
            print(f"[{_ts()}] 第 {i + 1} 秒查到（距平仓约 {delay}），共 {len(rows)} 条：")
            for r in rows:
                print(f"  {r.to_dict()}")
            break
        await asyncio.sleep(1)
    else:
        print("position_close 15 秒内未查到记录")
    if close_ts:
        # 生产回填路径（fill_sync）实际用的 _from/to 窗口：验证服务端是否真按窗口过滤
        lo, hi = int(close_ts) - 120, int(close_ts) + 5
        windowed = gw._api.list_position_close(gw._settle, contract=CONTRACT, _from=lo, to=hi)
        print(f"[{_ts()}] _from/to 窗口（{lo} ~ {hi}）返回 {len(windowed)} 条：")
        for r in windowed:
            print(f"  time={r.time} pnl={r.pnl} text={r.text}")


async def main() -> None:
    _, gw, conn = _build()
    _subscribe_private(conn)
    ws_task = asyncio.create_task(conn.run())
    close_ts: float | None = None
    try:
        await asyncio.sleep(3)  # 等订阅 ACK
        close_ts = await _open_then_trigger_tpsl(gw)
        _print_my_trades(gw)
        await _poll_position_close(gw, close_ts)
    finally:
        holding = [p for p in gw.list_positions() if p.contract == CONTRACT and p.size != 0]
        if holding:
            print(f"[{_ts()}] 兜底平仓：size={holding[0].size}")
            gw.place_order(OrderRequest(contract=CONTRACT, close=True))
        conn.close()
        ws_task.cancel()
        try:
            await ws_task
        except asyncio.CancelledError:
            pass
    print(
        "VERIFY PRIVATE FEED DONE：请把上方 usertrades/autoorders/position_close 原始字段贴回讨论"
    )


if __name__ == "__main__":
    asyncio.run(main())
