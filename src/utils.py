"""跨层共享的小工具：无业务依赖，任何层都可导入（避免高层反向依赖低层）。"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable
from dataclasses import dataclass
from decimal import Decimal, DivisionByZero, InvalidOperation, Overflow, localcontext


# ---------- LLM 调用身份：开轮快照落库（audit_rounds），供跨模型效果对比 ----------


@dataclass(frozen=True)
class LLMIdentity:
    """一次 LLM 调用实际使用的模型身份（凭证名/厂商/模型/思考强度）。

    全字段空串表示身份未知（历史轮次、测试桩或未注入身份的 provider）。
    """

    credential_name: str = ""
    provider: str = ""
    model: str = ""
    thinking_effort: str = ""


def identity_of(provider: object) -> LLMIdentity:
    """读取 provider 携带的模型身份；无 identity 属性（测试桩等）时返回全空身份。

    参数：
        provider: object，LLM provider 实例（鸭子类型，携带 identity 属性则读取）

    返回：
        LLMIdentity，provider 携带的模型身份；缺失或类型不符时为全空默认身份
    """
    identity = getattr(provider, "identity", None)
    return identity if isinstance(identity, LLMIdentity) else LLMIdentity()


async def maybe_await(result: Awaitable[None] | None) -> None:
    """统一等待可选协程结果，使回调可以同时支持同步与异步实现。

    参数：
        result: Awaitable[None] | None，回调返回的协程或同步空结果

    返回：
        None，存在可等待对象时等待其完成
    """
    if inspect.isawaitable(result):
        await result


# ---------- calc：安全数学表达式计算（Decimal，双 agent 的 calc 工具共用） ----------

_CALC_MAX_LEN = 200  # 表达式长度上限
_CALC_MAX_EXP = Decimal(1000)  # 指数绝对值上限（防恶意大幂）
_CALC_MAX_DEPTH = 50  # 括号嵌套深度上限（每层括号约 5 个栈帧，显式限幅防 RecursionError）
_CALC_ALLOWED = set("0123456789.+-*/^()Ee \t")  # tokenizer 之外的字符直接拒绝


class _CalcError(ValueError):
    """表达式解析/求值错误：message 即返回给 LLM 的中文错误文本。"""


def _scan_number(expr: str, start: int) -> int:
    """从 start 扫描数字字面量，返回结束位置；支持科学计数法（如 1.07E+301）。

    E 记法支持是为了闭环：calc 大数结果以 E 记法输出，LLM 可把结果代回继续计算。

    参数：
        expr: str，待扫描的数学表达式
        start: int，数字字面量的起始字符位置

    返回：
        int，数字字面量结束后的字符位置
    """
    j = start
    while j < len(expr) and expr[j] in "0123456789.":
        j += 1
    if j > start and j < len(expr) and expr[j] in "Ee":  # 指数部：E[+/-]digits
        k = j + 1
        if k < len(expr) and expr[k] in "+-":
            k += 1
        if k < len(expr) and expr[k].isdigit():
            j = k
            while j < len(expr) and expr[j].isdigit():
                j += 1
    return j


def _tokenize(expr: str) -> list[str]:
    """把表达式拆为数字和运算符词元，并把双星幂运算统一为脱字符。

    参数：
        expr: str，待拆分的数学表达式

    返回：
        list[str]，按原顺序排列的数字字面量与单字符运算符

    异常：
        _CalcError: 表达式包含不支持字符或无法识别的符号时抛出
    """
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
        else:  # 数字字面量；扫描未推进即非法（如孤立的 E），避免死循环
            j = _scan_number(expr, i)
            if j == i:
                raise _CalcError(f"无法识别的符号：{ch}")
            tokens.append(expr[i:j])
            i = j
    return tokens


class _Parser:
    """递归下降解析并求值：expr → term → factor(一元负号) → power(右结合) → atom。"""

    def __init__(self, tokens: list[str]) -> None:
        """初始化解析器：持有 token 序列，解析位置与括号嵌套深度归零。

        参数：
            tokens: list[str]，_tokenize 输出的 token 序列（数字字面量与单字符运算符）

        返回：
            None，就地初始化实例状态（_pos 与 _depth 归零）
        """
        self._tokens = tokens
        self._pos = 0
        self._depth = 0  # 括号嵌套深度（显式限幅，不依赖 Python 栈余量）

    def _peek(self) -> str | None:
        """查看当前待解析的 token，不推进解析位置。

        参数：无

        返回：
            str | None：当前 token；已读到序列末尾时返回 None
        """
        return self._tokens[self._pos] if self._pos < len(self._tokens) else None

    def _next(self) -> str:
        """取出当前 token 并推进解析位置。

        参数：无

        返回：
            str：当前 token

        异常：
            _CalcError：表达式已结束仍继续取词（表达式不完整）时抛出
        """
        token = self._peek()
        if token is None:
            raise _CalcError("表达式不完整（意外结束）")
        self._pos += 1
        return token

    def parse(self) -> Decimal:
        """解析完整表达式并求值，要求 token 序列恰好读完。

        参数：无

        返回：
            Decimal：表达式的计算结果

        异常：
            _CalcError：表达式解析完后仍残留多余 token 时抛出
        """
        value = self._expr()
        if self._peek() is not None:
            raise _CalcError(f"多余的内容：{self._peek()}")
        return value

    def _expr(self) -> Decimal:
        """解析加减层：若干乘除项以 +、- 连接，左结合求值。

        参数：无

        返回：
            Decimal：加减表达式的计算结果
        """
        value = self._term()
        while self._peek() in ("+", "-"):
            if self._next() == "+":
                value += self._term()
            else:
                value -= self._term()
        return value

    def _term(self) -> Decimal:
        """解析乘除层：若干一元因子以 *、/ 连接，左结合求值。

        参数：无

        返回：
            Decimal：乘除表达式的计算结果
        """
        value = self._factor()
        while self._peek() in ("*", "/"):
            if self._next() == "*":
                value *= self._factor()
            else:
                value /= self._factor()
        return value

    def _factor(self) -> Decimal:
        """解析一元负号层，使负号优先级低于幂运算并支持连续负号。

        参数：无

        返回：
            Decimal，一元因子求值结果
        """
        if self._peek() == "-":
            self._next()
            return -self._factor()
        return self._power()

    def _power(self) -> Decimal:
        """按右结合规则解析幂运算，并限制指数绝对值防止资源滥用。

        参数：无

        返回：
            Decimal，幂表达式求值结果

        异常：
            _CalcError: 指数绝对值超过允许上限时抛出
        """
        base = self._atom()
        if self._peek() != "^":
            return base
        self._next()
        exponent = self._factor()  # 指数经 factor：支持负指数，且天然右结合
        if abs(exponent) > _CALC_MAX_EXP:
            raise _CalcError(f"指数超出允许范围（|指数| ≤ {_CALC_MAX_EXP}）")
        return base**exponent

    def _atom(self) -> Decimal:
        """解析最小单元：括号子表达式或数字字面量。

        参数：无

        返回：
            Decimal：括号内子表达式的值，或数字字面量解析出的数值

        异常：
            _CalcError：括号嵌套超过 _CALC_MAX_DEPTH 层、缺少右括号，
                或 token 不是合法数字时抛出
        """
        token = self._next()
        if token == "(":
            self._depth += 1
            if self._depth > _CALC_MAX_DEPTH:
                raise _CalcError(f"括号嵌套过深（≤{_CALC_MAX_DEPTH} 层）")
            value = self._expr()
            if self._next() != ")":
                raise _CalcError("括号不匹配（缺少右括号）")
            self._depth -= 1
            return value
        try:
            return Decimal(token)
        except InvalidOperation:
            raise _CalcError(f"无法识别的数字或符号：{token}") from None


def _format_result(value: Decimal) -> str:
    """把 Decimal 结果格式化为去尾零文本，整数优先使用普通整数形式。

    参数：
        value: Decimal，待展示的计算结果

    返回：
        str，适合返回给 LLM 的紧凑数值文本
    """
    if value == value.to_integral_value() and value.adjusted() < 28:
        return str(value.quantize(Decimal(1)))
    return str(value.normalize())


# ---------- 指标数值文本格式化（执行/复盘两侧 get_indicators 共用） ----------


def fmt_indicator_value(value: str | None) -> str:
    """把指标原始值格式化为整数或两位小数，缺失与非数值分别降级展示。

    参数：
        value: str | None，指标原始文本或缺失值

    返回：
        str，格式化数值、原始非数值文本或“无数据”提示
    """
    if value is None:
        return "无数据"
    try:
        num = Decimal(value)
        if num == num.to_integral_value() and num.adjusted() < 28:
            return str(num.quantize(Decimal(1)))
        return format(num.quantize(Decimal("0.01")), "f")
    except InvalidOperation:  # 非数值或超精度：原样输出
        return value


def calc_expression(expr: str) -> str:
    """计算数学表达式（如 `2*(3-1)^2` → `8`）；任何错误返回中文文本，不抛异常。

    支持 + - * / ^（`**` 等价）、括号、一元负号与小数；全程 Decimal（prec=28），
    限幅 Emax 防大数溢出。供执行/复盘两侧 calc 工具 handler 直接返回给 LLM。

    参数：
        expr: str，待求值的数学表达式

    返回：
        str，计算结果文本；表达式非法、除零或溢出时返回中文错误说明
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
