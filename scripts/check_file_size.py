"""单文件行数门禁（对应 AGENTS.md 硬性规范 §2：单文件 ≤300 行，超 500 必拆）。

统计口径为「有效代码行」：总行数扣除空行、纯注释行与 docstring 行——
门禁限制的是代码复杂度而非文档厚度，补齐函数注释不会撑爆门禁。
- src/ 下 .py 文件有效代码行 >500 行：失败（exit 1，必须拆分）
- 300–500 行：仅警告（提示按规范逐步拆分，不阻塞）
- 基线豁免清单 BASELINE_OVERSIZE：登记启用新口径时已超标的文件；清单只允许缩小
  不允许新增，且豁免文件有效代码行不得继续增长（超过登记值即失败）

用法：uv run python scripts/check_file_size.py
"""

from __future__ import annotations

import ast
import io
import sys
import tokenize
from pathlib import Path

SOFT_LIMIT = 300  # 规范目标值：超出仅警告
HARD_LIMIT = 500  # 硬上限：超出即失败

# 基线豁免：路径 -> 新口径（有效代码行）启用时的行数；文件降到硬上限内后从清单移除
BASELINE_OVERSIZE: dict[str, int] = {}

ROOT = Path(__file__).resolve().parent.parent


def _docstring_lines(tree: ast.AST) -> set[int]:
    """收集语法树中全部模块、类和函数文档字符串占据的行号。

    参数：
        tree: ast.AST，待检查 Python 文件的抽象语法树

    返回：
        set[int]，全部文档字符串覆盖的行号集合
    """
    lines: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = node.body
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            lines.update(range(first.lineno, (first.end_lineno or first.lineno) + 1))
    return lines


def _comment_only_lines(text: str) -> set[int]:
    """收集仅含注释的行号，并保留带行尾注释的代码行。

    参数：
        text: str，待分析的 Python 源码文本

    返回：
        set[int]，井号前只有空白的纯注释行号集合
    """
    result: set[int] = set()
    source_lines = text.splitlines()
    for tok in tokenize.generate_tokens(io.StringIO(text).readline):
        if (
            tok.type == tokenize.COMMENT
            and not source_lines[tok.start[0] - 1][: tok.start[1]].strip()
        ):
            result.add(tok.start[0])
    return result


def effective_lines(path: Path) -> tuple[int, int]:
    """统计文件原始总行数与排除文档、空行和纯注释后的有效代码行数。

    参数：
        path: Path，待统计的 Python 文件路径

    返回：
        tuple[int, int]，依次为原始总行数和有效代码行数
    """
    text = path.read_text(encoding="utf-8")
    total = len(text.splitlines())
    ignored = _docstring_lines(ast.parse(text)) | _comment_only_lines(text)
    effective = sum(
        1
        for lineno, line in enumerate(text.splitlines(), 1)
        if line.strip() and lineno not in ignored
    )
    return total, effective


def main() -> int:
    """逐个统计 src/ 下 Python 文件的有效代码行数，按门禁规则输出警告与失败清单。

    规则：豁免清单内文件超过登记基线即失败、未超过则警告；其余文件超过硬上限
    HARD_LIMIT 失败，超过目标值 SOFT_LIMIT 仅警告。

    参数：无

    返回：
        int：进程退出码；存在失败文件时返回 1，否则返回 0
    """
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # Windows 控制台默认 GBK，统一 UTF-8
    warnings: list[str] = []
    failures: list[str] = []
    for path in sorted((ROOT / "src").rglob("*.py")):
        total, effective = effective_lines(path)
        rel = path.relative_to(ROOT).as_posix()
        detail = f"有效 {effective} 行 / 原始 {total} 行"
        baseline = BASELINE_OVERSIZE.get(rel)
        if baseline is not None:
            if effective > baseline:
                failures.append(f"{rel} {detail}（豁免基线 {baseline}，禁止继续增长，必须拆分）")
            else:
                warnings.append(f"{rel} {detail}（存量豁免，处置计划：拆分后移出清单）")
        elif effective > HARD_LIMIT:
            failures.append(f"{rel} {detail}（超过硬上限 {HARD_LIMIT}，必须拆分）")
        elif effective > SOFT_LIMIT:
            warnings.append(f"{rel} {detail}（规范目标 ≤{SOFT_LIMIT}，建议拆分）")

    for msg in warnings:
        print(f"警告：{msg}")
    for msg in failures:
        print(f"失败：{msg}")

    if failures:
        return 1
    print(
        f"单文件行数检查通过（有效代码行口径；警告 {len(warnings)} 个，无新增超 {HARD_LIMIT} 行文件）"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
