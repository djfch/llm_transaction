"""单文件行数门禁（对应 AGENTS.md 硬性规范 §2：单文件 ≤300 行，超 500 必拆）。

- src/ 下 .py 文件 >500 行：失败（exit 1，必须拆分）
- 300–500 行：仅警告（提示按规范逐步拆分，不阻塞）
- 存量豁免清单 BASELINE_OVERSIZE：建立门禁时已超标的文件，处置计划为拆分；
  清单只允许缩小不允许新增，且豁免文件行数不得继续增长（超过登记值即失败）

用法：uv run python scripts/check_file_size.py
"""

from __future__ import annotations

import sys
from pathlib import Path

SOFT_LIMIT = 300  # 规范目标值：超出仅警告
HARD_LIMIT = 500  # 硬上限：超出即失败

# 存量豁免：路径 -> 建立门禁时的行数（处置计划：拆分后从清单移除）
BASELINE_OVERSIZE = {
    "src/paper/engine.py": 543,
}

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # Windows 控制台默认 GBK，统一 UTF-8
    warnings: list[str] = []
    failures: list[str] = []
    for path in sorted((ROOT / "src").rglob("*.py")):
        lines = len(path.read_text(encoding="utf-8").splitlines())
        rel = path.relative_to(ROOT).as_posix()
        baseline = BASELINE_OVERSIZE.get(rel)
        if baseline is not None:
            if lines > baseline:
                failures.append(
                    f"{rel} 共 {lines} 行（豁免基线 {baseline}，禁止继续增长，必须拆分）"
                )
            else:
                warnings.append(f"{rel} 共 {lines} 行（存量豁免，处置计划：拆分后移出清单）")
        elif lines > HARD_LIMIT:
            failures.append(f"{rel} 共 {lines} 行（超过硬上限 {HARD_LIMIT}，必须拆分）")
        elif lines > SOFT_LIMIT:
            warnings.append(f"{rel} 共 {lines} 行（规范目标 ≤{SOFT_LIMIT}，建议拆分）")

    for msg in warnings:
        print(f"警告：{msg}")
    for msg in failures:
        print(f"失败：{msg}")

    if failures:
        return 1
    print(f"单文件行数检查通过（警告 {len(warnings)} 个，无新增超 {HARD_LIMIT} 行文件）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
