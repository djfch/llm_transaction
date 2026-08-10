"""手动验证脚本：调真实无签名接口 list_futures_contracts，打印前 5 个 USDT 永续合约。

不进测试套件（需要真实网络）。用法：uv run python scripts/check_gateway_public.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gate_api  # noqa: E402

from src.config import load_settings  # noqa: E402


def main() -> None:
    """调用 Gate 真实公共接口列出 USDT 永续合约，打印前 5 个用于手动验证连通性。

    参数：无

    返回：None，合约信息直接打印到控制台
    """
    gate = load_settings().gate
    client = gate_api.ApiClient(gate_api.Configuration(host=gate.live_host))
    api = gate_api.FuturesApi(client)
    contracts = api.list_futures_contracts(gate.settle)
    print(f"settle={gate.settle} 共 {len(contracts)} 个合约，前 5 个：")
    for c in contracts[:5]:
        print(
            f"  {c.name}: status={c.status} mark_price={c.mark_price} "
            f"funding_rate={c.funding_rate} quanto_multiplier={c.quanto_multiplier} "
            f"order_size_min={c.order_size_min} order_size_max={c.order_size_max} "
            f"enable_decimal={c.enable_decimal} in_delisting={c.in_delisting}"
        )


if __name__ == "__main__":
    main()
