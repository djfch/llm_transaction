"""手动验证脚本：testnet 真实开平仓 + 调杠杆闭环（沙盒资金，非真实资金）。

流程：读 .env 的 GATE_API_KEY/SECRET → 连接 testnet → 查账户 → 调杠杆 →
市价开多（最小张数）→ 校验持仓 → 平仓 → 校验持仓清空。
安全约束：强制断言 host 含 testnet，永不触实盘。
不进测试套件（需要真实网络与 testnet key）。用法：uv run python scripts/testnet_roundtrip.py
"""

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

from src.config import load_settings  # noqa: E402
from src.gateway.base import OrderRequest  # noqa: E402
from src.gateway.gate_rest import GateRestGateway  # noqa: E402

CONTRACT = "BTC_USDT"
LEVERAGE = 2


def _build_gateway() -> GateRestGateway:
    """加载配置与密钥，构建 testnet 网关；host 必须含 testnet，否则直接退出。"""
    load_dotenv()
    gate = load_settings().gate
    if "testnet" not in gate.testnet_host:
        sys.exit(f"拒绝执行：testnet_host 不含 testnet 字样 -> {gate.testnet_host}")
    key, secret = os.environ.get("GATE_API_KEY", ""), os.environ.get("GATE_API_SECRET", "")
    if not key or not secret:
        sys.exit("拒绝执行：.env 未配置 GATE_API_KEY/GATE_API_SECRET")
    return GateRestGateway(gate, api_key=key, api_secret=secret, testnet=True)


def _step_account(gw: GateRestGateway) -> None:
    acc = gw.get_account()
    print(
        f"[1/6] 账户：available(可用)={acc.available} unrealised_pnl(未实现盈亏)={acc.unrealised_pnl}"
    )


def _step_leverage(gw: GateRestGateway) -> None:
    pos = gw.set_leverage(CONTRACT, LEVERAGE, "isolated")
    print(f"[2/6] 调杠杆：{CONTRACT} leverage(杠杆)={pos.leverage} 模式=isolated")


def _step_open(gw: GateRestGateway, size: int) -> None:
    result = gw.place_order(OrderRequest(contract=CONTRACT, size=size))
    print(
        f"[3/6] 市价开多 {size} 张：status={result.status} "
        f"fill_price(成交均价)={result.fill_price} text={result.text}"
    )


def _step_verify_position(gw: GateRestGateway) -> None:
    time.sleep(2)  # 等撮合落定
    holding = [p for p in gw.list_positions() if p.contract == CONTRACT and p.size != 0]
    if not holding:
        sys.exit(f"失败：开仓后 {CONTRACT} 无持仓")
    p = holding[0]
    print(
        f"[4/6] 持仓校验：size(张数)={p.size} entry_price(开仓价)={p.entry_price} "
        f"liq_price(强平估值)={p.liq_price}"
    )


def _step_close(gw: GateRestGateway) -> None:
    result = gw.place_order(OrderRequest(contract=CONTRACT, close=True))
    print(f"[5/6] 平仓（size=0+close=true）：status={result.status} finish_as={result.finish_as}")


def _step_verify_flat(gw: GateRestGateway) -> None:
    time.sleep(2)
    holding = [p for p in gw.list_positions() if p.contract == CONTRACT and p.size != 0]
    if holding:
        sys.exit(f"失败：平仓后仍有持仓 size={holding[0].size}")
    print("[6/6] 平仓校验：无残留持仓")


def main() -> None:
    gw = _build_gateway()
    contract = gw.get_contract(CONTRACT)
    size = int(contract.order_size_min)
    print(
        f"合约 {CONTRACT}: mark_price={contract.mark_price} "
        f"order_size_min(最小张数)={size} quanto_multiplier={contract.quanto_multiplier}"
    )
    _step_account(gw)
    _step_leverage(gw)
    _step_open(gw, size)
    _step_verify_position(gw)
    _step_close(gw)
    _step_verify_flat(gw)
    print("TESTNET ROUNDTRIP PASS：调杠杆 + 开平仓闭环完成")


if __name__ == "__main__":
    main()
