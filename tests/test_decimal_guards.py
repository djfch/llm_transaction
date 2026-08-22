"""LLM 参数数值与交易所参数格式化的极端值防护测试（issue #80）。"""

from decimal import Decimal

import pytest

from src.agent.tool_handlers import ToolArgError, _to_decimal
from src.gateway.base import GatewayError
from src.gateway.gate_rest import _fmt_decimal


def test_to_decimal_accepts_normal_values():
    """验证正常价格/张数值不受防护影响。

    参数：无

    返回：
        None，断言整数、小数、科学计数法常规值均正常转换
    """
    assert _to_decimal("50000", "price") == Decimal("50000")
    assert _to_decimal("0.00000001", "price") == Decimal("0.00000001")
    assert _to_decimal(10, "size") == Decimal("10")


@pytest.mark.parametrize(
    "bad",
    ["NaN", "Infinity", "-Infinity", "1e-1000000000", "1e1000000000", "1234567890123456789"],
)
def test_to_decimal_rejects_extreme_values(bad):
    """验证非有限值、极端指数与超精度值被拒绝且不产生超长展开。

    参数：
        bad: str，非法参数值

    返回：
        None，断言全部抛 ToolArgError（issue #80：极端指数格式化会耗尽内存）
    """
    with pytest.raises(ToolArgError):
        _to_decimal(bad, "price")


def test_fmt_decimal_formats_normally():
    """验证网关层格式化对常规值行为不变。

    参数：无

    返回：
        None，断言普通十进制输出（无科学计数法）
    """
    assert _fmt_decimal(Decimal("50000")) == "50000"
    assert _fmt_decimal(Decimal("0.1")) == "0.1"


@pytest.mark.parametrize("bad", ["1e-1000000000", "Infinity", "1234567890123456789"])
def test_fmt_decimal_refuses_dangerous_expansion(bad):
    """验证网关层兜底：危险值在 format 展开前抛 GatewayError。

    参数：
        bad: str，危险数值

    返回：
        None，断言抛 GatewayError 且不执行等长字符串展开（issue #80 第二道闸）
    """
    with pytest.raises(GatewayError):
        _fmt_decimal(Decimal(bad))
