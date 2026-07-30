"""跨层共享的小工具：无业务依赖，任何层都可导入（避免高层反向依赖低层）。"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable
from decimal import Decimal, DivisionByZero, InvalidOperation, Overflow, localcontext


async def maybe_await(result: Awaitable[None] | None) -> None:
    """处理函数允许同步或协程，统一在此消化。"""
    if inspect.isawaitable(result):
        await result


# ---------- calc：安全数学表达式计算（Decimal，双 agent 的 calc 工具共用） ----------

_CALC_MAX_LEN = 200  # 表达式长度上限
_CALC_MAX_EXP = Decimal(1000)  # 指数绝对值上限（防恶意大幂）
_CALC_ALLOWED = set("0123456789.+-*/^() \t")  # tokenizer 之外的字符直接拒绝


class _CalcError(ValueError):
    """表达式解析/求值错误：message 即返回给 LLM 的中文错误文本。"""


def _tokenize(expr: str) -> list[str]:
    """拆为 token 序列：数字字面量与单字符运算符（`**` 先归一为 `^`）。"""
    expr = expr.replace("**", "^")
    if bad := set(expr) - _CALC_ALLOWED:
        raise _CalcError(f"包含不支持的字符：{''.join(sorted(bad))}")
    tokens: list[str] = []
    i = 0
    while i < len(expr):
        ch = expr[i]
        if ch in " \t":
            i += 1
        elif ch in "+-*/^()":
            tokens.append(ch)
            i += 1
        else:  # 数字字面量：连续的数字与小数点，交给 Decimal 做最终合法性校验
            j = i
            while j < len(expr) and expr[j] in "0123456789.":
                j += 1
            tokens.append(expr[i:j])
            i = j
    return tokens


class _Parser:
    """递归下降解析并求值：expr → term → factor(一元负号) → power(右结合) → atom。"""

    def __init__(self, tokens: list[str]) -> None:
        self._tokens = tokens
        self._pos = 0

    def _peek(self) -> str | None:
        return self._tokens[self._pos] if self._pos < len(self._tokens) else None

    def _next(self) -> str:
        token = self._peek()
        if token is None:
            raise _CalcError("表达式不完整（意外结束）")
        self._pos += 1
        return token

    def parse(self) -> Decimal:
        value = self._expr()
        if self._peek() is not None:
            raise _CalcError(f"多余的内容：{self._peek()}")
        return value

    def _expr(self) -> Decimal:
        value = self._term()
        while self._peek() in ("+", "-"):
            if self._next() == "+":
                value += self._term()
            else:
                value -= self._term()
        return value

    def _term(self) -> Decimal:
        value = self._factor()
        while self._peek() in ("*", "/"):
            if self._next() == "*":
                value *= self._factor()
            else:
                value /= self._factor()
        return value

    def _factor(self) -> Decimal:
        """一元负号层：`-x^2` 按惯例解释为 `-(x^2)`。"""
        if self._peek() == "-":
            self._next()
            return -self._factor()
        return self._power()

    def _power(self) -> Decimal:
        """幂运算，右结合：`2^3^2` = `2^(3^2)` = 512。"""
        base = self._atom()
        if self._peek() != "^":
            return base
        self._next()
        exponent = self._factor()  # 指数经 factor：支持负指数，且天然右结合
        if abs(exponent) > _CALC_MAX_EXP:
            raise _CalcError(f"指数超出允许范围（|指数| ≤ {_CALC_MAX_EXP}）")
        return base**exponent

    def _atom(self) -> Decimal:
        token = self._next()
        if token == "(":
            value = self._expr()
            if self._next() != ")":
                raise _CalcError("括号不匹配（缺少右括号）")
            return value
        try:
            return Decimal(token)
        except InvalidOperation:
            raise _CalcError(f"无法识别的数字或符号：{token}") from None


def _format_result(value: Decimal) -> str:
    """结果去尾零：整数值显示为整数形式（8 而非 8.000），超精度回退科学计数法。"""
    if value == value.to_integral_value() and value.adjusted() < 28:
        return str(value.quantize(Decimal(1)))
    return str(value.normalize())


def calc_expression(expr: str) -> str:
    """计算数学表达式（如 `2*(3-1)^2` → `8`）；任何错误返回中文文本，不抛异常。

    支持 + - * / ^（`**` 等价）、括号、一元负号与小数；全程 Decimal（prec=28），
    限幅 Emax 防大数溢出。供执行/复盘两侧 calc 工具 handler 直接返回给 LLM。
    """
    expr = expr.strip()
    if not expr:
        return "计算错误：表达式为空"
    if len(expr) > _CALC_MAX_LEN:
        return f"计算错误：表达式过长（≤{_CALC_MAX_LEN} 字符）"
    try:
        with localcontext() as ctx:
            ctx.prec = 28
            ctx.Emax = 10**6
            ctx.Emin = -(10**6)
            result = _Parser(_tokenize(expr)).parse()
            return _format_result(result)
    except _CalcError as e:
        return f"计算错误：{e}"
    except (DivisionByZero, ZeroDivisionError):
        return "计算错误：除数为零"
    except Overflow:
        return "计算错误：结果超出可表示范围"
    except InvalidOperation:
        return "计算错误：非法运算（如负数的非整数次幂）"
