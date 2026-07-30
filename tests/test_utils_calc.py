"""calc_expression 计算器测试：四则/幂/括号/精度/防护 + 执行 agent 注册表集成。

calc 是纯函数工具：执行侧集成用例以空依赖构造 ToolDeps，确认 registry.execute
路径可达且不经风控（risk_verdict 空串）。
"""

from types import SimpleNamespace

from src.agent.tool_handlers import ToolDeps
from src.agent.tools import ToolRegistry
from src.utils import calc_expression

# ---------- 基本运算 ----------


def test_basic_arithmetic():
    assert calc_expression("1+2") == "3"
    assert calc_expression("10-3*2") == "4"
    assert calc_expression("7/2") == "3.5"
    assert calc_expression("2*(3-1)^2") == "8"  # 用户给定的验收样例


def test_power_right_associative():
    """幂右结合：2^3^2 = 2^(3^2) = 512；** 与 ^ 等价。"""
    assert calc_expression("2^3^2") == "512"
    assert calc_expression("2**3**2") == "512"
    assert calc_expression("2^-1") == "0.5"


def test_unary_minus():
    assert calc_expression("-3+5") == "2"
    assert calc_expression("-(2+3)") == "-5"
    assert calc_expression("-2^2") == "-4"  # 惯例：-(2^2)


def test_decimal_precision():
    """Decimal 精度：0.1+0.2 精确等于 0.3（float 会是 0.30000000000000004）。"""
    assert calc_expression("0.1+0.2") == "0.3"
    assert calc_expression("1.5*2") == "3"  # 去尾零


def test_nested_parens_and_spaces():
    assert calc_expression(" (1 + 2) * (3 + (4 - 2)) ") == "15"


# ---------- 错误路径（返回中文文本，不抛异常） ----------


def test_divide_by_zero():
    assert "除数为零" in calc_expression("1/0")


def test_syntax_errors():
    assert "计算错误" in calc_expression("2*")
    assert "计算错误" in calc_expression("(1+2")
    assert "计算错误" in calc_expression("1..2+3")
    assert "计算错误" in calc_expression("")


def test_illegal_characters():
    out = calc_expression("__import__('os')")
    assert "不支持的字符" in out


def test_malformed_operator_combos():
    """`**`→`^` 归一后的畸形组合与孤立 E 均转错误文本。"""
    assert "计算错误" in calc_expression("2*^3")
    assert "计算错误" in calc_expression("2^^3")
    assert "计算错误" in calc_expression("E+3")


def test_scientific_notation_round_trip():
    """E 记法闭环：大数结果可作为输入代回继续计算。"""
    assert calc_expression("1.5E+3+500") == "2000"
    assert calc_expression("1e2*2") == "200"


def test_deep_nesting_returns_text_not_exception():
    """括号嵌套限幅：50 层可算，超限/纯左括号洪水均返回文本而非 RecursionError。"""
    assert calc_expression("(" * 50 + "1" + ")" * 50) == "1"
    assert "嵌套过深" in calc_expression("(" * 51 + "1" + ")" * 51)
    assert "计算错误" in calc_expression("(" * 100)  # 不抛异常


def test_long_unary_minus_chain():
    """长一元负号链：不穿透递归限制（长度上限先拦住更长输入）。"""
    assert calc_expression("-" * 99 + "1") in ("1", "-1")


def test_too_long_expression():
    assert "过长" in calc_expression("1+" * 101 + "1")


def test_exponent_limit():
    assert "指数超出允许范围" in calc_expression("2^1001")
    assert calc_expression("2^1000").startswith("1.07")  # 上限内可算（科学计数法）


def test_negative_base_fraction_power():
    assert "计算错误" in calc_expression("(-8)^0.5")


# ---------- 执行 agent 注册表集成：calc 可达且不经风控 ----------


def _dummy_deps() -> ToolDeps:
    """calc 不触碰任何依赖：全空占位即可构造注册表。"""
    none = SimpleNamespace()
    return ToolDeps(
        gateway=none,
        risk_engine=none,
        risk_config=none,
        watchlist=[],
        repo=none,
        candles=none,
        triggers=none,
        daily_stats_fn=None,
    )


async def test_agent_registry_calc():
    registry = ToolRegistry(_dummy_deps())
    out = await registry.execute("calc", {"expression": "2*(3-1)^2"})
    assert out.text == "8"
    assert out.risk_verdict == ""  # 纯计算工具不经风控


async def test_agent_registry_calc_missing_arg():
    registry = ToolRegistry(_dummy_deps())
    out = await registry.execute("calc", {})
    assert "参数错误" in out.text
