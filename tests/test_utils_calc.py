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
    """校验基本四则运算、运算符优先级与括号幂混合表达式的计算结果。

    参数：无

    返回：
        None，断言加减乘除、优先级及 2*(3-1)^2=8 验收样例的结果字符串正确
    """
    assert calc_expression("1+2") == "3"
    assert calc_expression("10-3*2") == "4"
    assert calc_expression("7/2") == "3.5"
    assert calc_expression("2*(3-1)^2") == "8"  # 用户给定的验收样例


def test_power_right_associative():
    """幂右结合：2^3^2 = 2^(3^2) = 512；** 与 ^ 等价。

    参数：
        无

    返回：
        None：通过断言校验目标场景，无返回值
    """
    assert calc_expression("2^3^2") == "512"
    assert calc_expression("2**3**2") == "512"
    assert calc_expression("2^-1") == "0.5"


def test_unary_minus():
    """校验一元负号的求值与优先级：负号可前缀数字或括号，且 -2^2 按惯例得 -4。

    参数：无

    返回：
        None，断言三个含负号表达式的结果分别为 2、-5、-4
    """
    assert calc_expression("-3+5") == "2"
    assert calc_expression("-(2+3)") == "-5"
    assert calc_expression("-2^2") == "-4"  # 惯例：-(2^2)


def test_decimal_precision():
    """Decimal 精度：0.1+0.2 精确等于 0.3（float 会是 0.30000000000000004）。

    参数：
        无

    返回：
        None：通过断言校验目标场景，无返回值
    """
    assert calc_expression("0.1+0.2") == "0.3"
    assert calc_expression("1.5*2") == "3"  # 去尾零


def test_nested_parens_and_spaces():
    """计算器忽略表达式空格并正确求值多层括号。

    参数：
        无

    返回：
        None：通过断言校验目标场景，无返回值
    """
    assert calc_expression(" (1 + 2) * (3 + (4 - 2)) ") == "15"


# ---------- 错误路径（返回中文文本，不抛异常） ----------


def test_divide_by_zero():
    """除数为零时返回中文错误文本而不向外抛异常。

    参数：
        无

    返回：
        None：通过断言校验目标场景，无返回值
    """
    assert "除数为零" in calc_expression("1/0")


def test_syntax_errors():
    """残缺运算符、括号、非法小数和空表达式统一返回计算错误。

    参数：
        无

    返回：
        None：通过断言校验目标场景，无返回值
    """
    assert "计算错误" in calc_expression("2*")
    assert "计算错误" in calc_expression("(1+2")
    assert "计算错误" in calc_expression("1..2+3")
    assert "计算错误" in calc_expression("")


def test_illegal_characters():
    """包含函数调用等非法字符的表达式在解析前被拒绝。

    参数：
        无

    返回：
        None：通过断言校验目标场景，无返回值
    """
    out = calc_expression("__import__('os')")
    assert "不支持的字符" in out


def test_malformed_operator_combos():
    """`**`→`^` 归一后的畸形组合与孤立 E 均转错误文本。

    参数：
        无

    返回：
        None：通过断言校验目标场景，无返回值
    """
    assert "计算错误" in calc_expression("2*^3")
    assert "计算错误" in calc_expression("2^^3")
    assert "计算错误" in calc_expression("E+3")


def test_scientific_notation_round_trip():
    """E 记法闭环：大数结果可作为输入代回继续计算。

    参数：
        无

    返回：
        None：通过断言校验目标场景，无返回值
    """
    assert calc_expression("1.5E+3+500") == "2000"
    assert calc_expression("1e2*2") == "200"


def test_deep_nesting_returns_text_not_exception():
    """括号嵌套限幅：50 层可算，超限/纯左括号洪水均返回文本而非 RecursionError。

    参数：
        无

    返回：
        None：通过断言校验目标场景，无返回值
    """
    assert calc_expression("(" * 50 + "1" + ")" * 50) == "1"
    assert "嵌套过深" in calc_expression("(" * 51 + "1" + ")" * 51)
    assert "计算错误" in calc_expression("(" * 100)  # 不抛异常


def test_long_unary_minus_chain():
    """长一元负号链：不穿透递归限制（长度上限先拦住更长输入）。

    参数：
        无

    返回：
        None：通过断言校验目标场景，无返回值
    """
    assert calc_expression("-" * 99 + "1") in ("1", "-1")


def test_too_long_expression():
    """超过长度上限的表达式返回过长提示。

    参数：
        无

    返回：
        None：通过断言校验目标场景，无返回值
    """
    assert "过长" in calc_expression("1+" * 101 + "1")


def test_exponent_limit():
    """指数 1000 可计算，超过上限则返回范围错误。

    参数：
        无

    返回：
        None：通过断言校验目标场景，无返回值
    """
    assert "指数超出允许范围" in calc_expression("2^1001")
    assert calc_expression("2^1000").startswith("1.07")  # 上限内可算（科学计数法）


def test_negative_base_fraction_power():
    """负数底数进行小数次幂运算时返回计算错误。

    参数：
        无

    返回：
        None：通过断言校验目标场景，无返回值
    """
    assert "计算错误" in calc_expression("(-8)^0.5")


# ---------- 执行 agent 注册表集成：calc 可达且不经风控 ----------


def _dummy_deps() -> ToolDeps:
    """calc 不触碰任何依赖：全空占位即可构造注册表。

    参数：
        无

    返回：
        ToolDeps：仅为注册 calc 工具而组装的空依赖集合
    """
    none = SimpleNamespace()
    return ToolDeps(
        gateway=none,
        risk_engine=none,
        risk_config=none,
        watchlist=[],
        repo=none,
        candles=none,
        triggers=none,
        indicator_service=None,
        daily_stats_fn=None,
    )


async def test_agent_registry_calc():
    """工具注册表可执行 calc，并将其作为无需风控的纯计算工具。

    参数：
        无

    返回：
        None：通过断言校验目标场景，无返回值
    """
    registry = ToolRegistry(_dummy_deps())
    out = await registry.execute("calc", {"expression": "2*(3-1)^2"})
    assert out.text == "8"
    assert out.risk_verdict == ""  # 纯计算工具不经风控


async def test_agent_registry_calc_missing_arg():
    """calc 工具缺少 expression 参数时返回参数错误。

    参数：
        无

    返回：
        None：通过断言校验目标场景，无返回值
    """
    registry = ToolRegistry(_dummy_deps())
    out = await registry.execute("calc", {})
    assert "参数错误" in out.text
