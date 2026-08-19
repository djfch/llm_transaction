"""架构守护：async 路径不得直接调用同步网关方法，必须经统一卸载层 run_gateway_io。

AST 扫描 src/（gateway 网关实现自身与 paper 纯内存模拟豁免）：async def 内出现
- `*.gateway.*(...)` / `*._gateway.*(...)` 形式的直接调用；
- `_require_gateway(deps).*(...)` 形式的直接调用；
- `asyncio.to_thread(...)` 携带网关方法引用；
即判定违规。模块级同步辅助函数内部的网关调用不违规（约定：整体经卸载层调用）。
已知盲区：在 async 中直接调用"含网关读取的同步辅助函数"本扫描无法识别，靠评审兜底。
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC_ROOT = Path(__file__).resolve().parent.parent / "src"
_WHITELIST_DIRS = frozenset({"gateway", "paper"})  # 网关实现自身 / 纯内存模拟（无网络 I/O）
_HIT_NAMES = frozenset({"gateway", "_gateway"})  # 属性链中的网关持有名


def _chain(node: ast.expr) -> list[str]:
    """把属性链表达式展平为名字列表（deps.gateway.list_positions → [deps, gateway, list_positions]）。

    参数：
        node: ast.expr，待展平的表达式节点（Attribute/Name 链，遇其他节点截断）

    返回：
        list[str]：自左向右的名字序列（非属性链部分被截断，可能只剩尾部一段）
    """
    names: list[str] = []
    while isinstance(node, ast.Attribute):
        names.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        names.append(node.id)
    names.reverse()
    return names


class _AsyncGatewayCallScanner(ast.NodeVisitor):
    """收集 async 上下文里的直接同步网关调用。"""

    def __init__(self) -> None:
        """初始化空违规列表与 async 深度计数。

        参数：无
        返回：
            None，就地初始化实例属性
        """
        self.violations: list[tuple[int, str]] = []
        self._async_depth = 0

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        """进入/离开 async 函数作用域（嵌套同步 def 不豁免：其仍在事件循环线程执行）。

        参数：
            node: ast.AsyncFunctionDef，async 函数定义节点

        返回：
            None，维护 async 深度并继续遍历
        """
        self._async_depth += 1
        self.generic_visit(node)
        self._async_depth -= 1

    def visit_Call(self, node: ast.Call) -> None:
        """检查调用节点是否为 async 上下文中的直接网关调用。

        参数：
            node: ast.Call，调用表达式节点

        返回：
            None，命中时向违规列表追加（行号, 调用描述）
        """
        if self._async_depth > 0 and _is_gateway_call(node):
            self.violations.append((node.lineno, ".".join(_chain(node.func))))
        self.generic_visit(node)


def _is_gateway_call(node: ast.Call) -> bool:
    """判定调用节点是否触达同步网关方法（含 asyncio.to_thread 携带网关引用）。

    参数：
        node: ast.Call，调用表达式节点

    返回：
        bool：直接网关调用或 to_thread 携带网关方法引用时为 True
    """
    chain = _chain(node.func)
    if len(chain) >= 2 and any(name in _HIT_NAMES for name in chain[:-1]):
        return True
    # _require_gateway(deps).method(...) 形态：func 是 Attribute，其 value 为 _require_gateway 调用
    value = node.func.value if isinstance(node.func, ast.Attribute) else None
    if isinstance(value, ast.Call) and _chain(value.func)[:1] == ["_require_gateway"]:
        return True
    # asyncio.to_thread(gateway 方法引用, ...)：issue #72 后统一走 run_gateway_io
    if chain[-2:] == ["asyncio", "to_thread"] and node.args:
        return any(name in _HIT_NAMES for name in _chain(node.args[0]))
    return False


def _scan_source(source: str) -> list[tuple[int, str]]:
    """扫描一段 Python 源码，返回 async 上下文中的直接网关调用列表。

    参数：
        source: str，Python 源码文本

    返回：
        list[tuple[int, str]]：（行号, 调用描述）违规列表
    """
    scanner = _AsyncGatewayCallScanner()
    scanner.visit(ast.parse(source))
    return scanner.violations


def test_no_direct_gateway_calls_in_async_paths():
    """全量扫描 src/：async 路径不得存在直接同步网关调用（issue #72 守护）。

    参数：无

    返回：
        None，断言所有被扫文件均无违规；命中时逐文件逐行列出
    """
    violations: list[str] = []
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        if _WHITELIST_DIRS & set(path.relative_to(_SRC_ROOT).parts[:-1]):
            continue
        for lineno, call in _scan_source(path.read_text(encoding="utf-8")):
            violations.append(f"{path.relative_to(_SRC_ROOT)}:{lineno}: {call}")
    assert violations == [], "async 路径存在未卸载的直接网关调用：\n" + "\n".join(violations)


def test_scanner_flags_direct_gateway_call():
    """验证扫描器能识别 async 函数内的直接网关调用（防守护自身退化）。

    参数：无

    返回：
        None，断言违规样例被逐条命中
    """
    source = (
        "async def f(deps):\n"
        "    positions = deps.gateway.list_positions()\n"
        "    meta = await asyncio.to_thread(deps.gateway.get_contract, 'BTC_USDT')\n"
        "    account = _require_gateway(deps).get_account()\n"
    )
    violations = _scan_source(source)
    assert [line for line, _ in violations] == [2, 3, 4]


def test_scanner_allows_offloaded_and_sync_helpers():
    """验证 run_gateway_io 包裹、同步辅助函数内部调用、以及非网关调用不被误伤。

    参数：无

    返回：
        None，断言合规样例零违规
    """
    source = (
        "async def f(deps):\n"
        "    positions = await run_gateway_io(deps.gateway.list_positions)\n"
        "    equity = compute_equity(account, positions)\n"
        "\n"
        "def sync_helper(gateway):\n"
        "    return gateway.get_account()\n"
    )
    assert _scan_source(source) == []
