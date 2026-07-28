"""review_prompt.md 加载（mtime 热重载）+ 复盘工具说明段渲染。

与 src/agent/prompts.py 同模式但独立实现（本包不 import src/agent/*，允许少量重复）：
每次取 system prompt 时检查文件 mtime，变更即重读；工具说明段由
render_tool_docs 按注册表 specs（鸭子类型：有 name/description/parameters 属性）生成。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Protocol

from src.audit.logger import get_logger

logger = get_logger(__name__)


class _ToolSpecLike(Protocol):
    """工具说明渲染所需的最小结构（鸭子类型，不绑定任何具体注册表）。"""

    name: str
    description: str
    parameters: dict[str, Any]


class ReviewPromptLoader:
    """复盘提示词加载器：缓存 + mtime 检测热重载。

    与 PromptLoader 的差异：首次加载即缺文件时不抛错，返回空正文并 logger.warning
    一次（不重复骚扰；复盘提示词允许缺省，工具说明段仍可组成可用 prompt）；
    运行中被删除则沿用缓存。
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._mtime: float | None = None
        self._body: str = ""
        self._warned_missing = False

    def _load_body(self) -> str:
        try:
            mtime = self._path.stat().st_mtime
        except FileNotFoundError:
            if not self._warned_missing:
                logger.warning(
                    "复盘提示词文件缺失：%s（使用空正文；可复制 review_prompt.example.md 创建）",
                    self._path,
                )
                self._warned_missing = True
            return self._body  # 缺失：返回缓存（首次加载时缓存即空串）
        if mtime != self._mtime:
            self._body = self._path.read_text(encoding="utf-8")
            self._mtime = mtime
        return self._body

    def system_prompt(self, tool_docs: str) -> tuple[str, str]:
        """返回（完整 system prompt, md5）。完整文本 = 复盘提示词正文 + 工具说明段。"""
        body = self._load_body()
        full = body.rstrip() + "\n\n" + tool_docs
        return full, hashlib.md5(full.encode("utf-8")).hexdigest()


def render_tool_docs(specs: list[_ToolSpecLike]) -> str:
    """工具说明段：名称 + 描述 + 必填参数（schema 明细经 API tools 字段单独下发）。"""
    lines = ["## 可用工具", ""]
    for t in specs:
        required = "、".join(t.parameters.get("required", [])) or "无"
        lines.append(f"- `{t.name}`：{t.description}（必填参数：{required}）")
    return "\n".join(lines)
