"""paper 新增敞口前刷新 Gate 合约规格的测试。"""

from decimal import Decimal

from src.config import PaperConfig
from src.gateway.base import Contract
from src.paper.engine import PaperGateway

D = Decimal


def _contract(multiplier: str) -> Contract:
    """构造指定合约乘数的测试规格。

    参数：
        multiplier: str，每张对应币数

    返回：
        Contract：可交易的 BTC_USDT 合约规格
    """
    return Contract(
        name="BTC_USDT",
        quanto_multiplier=D(multiplier),
        order_size_min=D(1),
        order_size_max=D(1000),
        order_price_round=D("0.1"),
        enable_decimal=False,
        mark_price=D(100),
        funding_rate=D(0),
        funding_interval=28800,
        maker_fee_rate=D("0.0002"),
        taker_fee_rate=D("0.0005"),
        status="trading",
        in_delisting=False,
    )


def test_refresh_contract_reads_provider_and_updates_memory():
    """校验 paper 刷新调用公共提供方，并用新规格覆盖内存旧值。

    参数：无

    返回：
        None，断言提供方被调用一次且后续内存读取返回新乘数
    """
    calls: list[str] = []

    def provider(contract: str) -> Contract:
        """返回更新后的合约规格并记录调用。

        参数：
            contract: str，合约名

        返回：
            Contract：乘数更新为 0.2 的规格
        """
        calls.append(contract)
        return _contract("0.2")

    gateway = PaperGateway(
        PaperConfig(),
        contracts={"BTC_USDT": _contract("0.1")},
        contract_provider=provider,
    )
    assert gateway.refresh_contract("BTC_USDT").quanto_multiplier == D("0.2")
    assert gateway.get_contract("BTC_USDT").quanto_multiplier == D("0.2")
    assert calls == ["BTC_USDT"]


def test_refresh_contract_without_provider_uses_injected_test_spec():
    """校验离线 mock paper 未注入公共提供方时仍可使用显式测试规格。

    参数：无

    返回：
        None，断言刷新结果等于内存规格
    """
    gateway = PaperGateway(PaperConfig(), contracts={"BTC_USDT": _contract("0.1")})
    assert gateway.refresh_contract("BTC_USDT").quanto_multiplier == D("0.1")
