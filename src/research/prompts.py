"""research_prompt.md 加载（mtime 热重载）+ 研报工具说明段渲染。

与 src/review/prompts.py 同模式但独立实现（本包不 import src/review/*，
允许少量重复）：每次取 system prompt 时检查文件 mtime，变更即重读；
工具说明段由 render_tool_docs 按注册表 specs（鸭子类型）生成。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Protocol

from src.audit.logger import get_logger

logger = get_logger(__name__)

RESEARCH_PROTOCOL_V2 = """## 强制协议附录：RESEARCH_ASSET_VIEWS_PROTOCOL_V2

本附录优先级高于运行时策略正文中的旧输出格式。
- 本轮白名单已冻结。必须对每个合约恰好调用一次 get_research_market_data，可用 limit 控制
  同时返回的 4h 与 1d K 线根数；不得查询白名单外合约。
- 1d 判断结构方向，4h 判断近期节奏。新闻和宏观数据解释催化剂；K 线、成交量、持仓量与
  资金费率验证市场是否真正响应。
- 新闻与技术结构冲突时降低置信度，并把 technical_confirmation 写为 冲突。
- 无重要新闻时允许 basis_type=结构延续，但 confidence 最高只能为 中。
- 行情不可用仍须给该合约输出 direction=中性、confidence=低、technical_confirmation=不可用。
- 最终只输出一个 JSON 对象，根字段必须为 summary、cross_market_view、global_risks、
  asset_views。asset_views 每项必须含 contract、direction、confidence、horizon、
  market_regime、technical_confirmation、basis_type、evidence、risks、narrative。
- 白名单集合、成功调用市场工具的合约集合、asset_views 合约集合必须完全相等，不得遗漏、
  重复或增加未知合约。
"""


class _ToolSpecLike(Protocol):
    """工具说明渲染所需的最小结构（鸭子类型，不绑定任何具体注册表）。"""

    name: str
    description: str
    parameters: dict[str, Any]


class ResearchPromptLoader:
    """研报提示词加载器：缓存 + mtime 检测热重载。

    首次加载即缺文件时不抛错，返回空正文并 logger.warning 一次
    （研报提示词允许缺省，工具说明段仍可组成可用 prompt）；
    运行中被删除则沿用缓存。
    """

    def __init__(self, path: str | Path) -> None:
        """初始化加载器，记录研报提示词文件路径并清空缓存状态。

        参数：
            path: str | Path，research_prompt.md 的文件路径

        返回：
            None，仅初始化实例属性（正文缓存为空，尚未读取文件）
        """
        self._path = Path(path)
        self._mtime: float | None = None
        self._body: str = ""
        self._warned_missing = False

    def _load_body(self) -> str:
        """读取研报提示词正文：文件 mtime 变化时重读并更新缓存，实现热重载。

        参数：无

        返回：
            str：提示词正文；文件缺失时返回缓存正文（首次缺失为空字符串，
            并警告一次；运行中被删除则沿用旧缓存）
        """
        try:
            mtime = self._path.stat().st_mtime
        except FileNotFoundError:
            if not self._warned_missing:
                logger.warning(
                    "研报提示词文件缺失：%s（使用空正文；可复制 research_prompt.example.md 创建）",
                    self._path,
                )
                self._warned_missing = True
            return self._body
        if mtime != self._mtime:
            self._body = self._path.read_text(encoding="utf-8")
            self._mtime = mtime
        return self._body

    def system_prompt(self, tool_docs: str) -> tuple[str, str]:
        """返回（完整 system prompt, md5）。完整文本 = 研报提示词正文 + 工具说明段。

        参数：
            tool_docs: str，渲染后的工具说明文本
        返回：
            tuple[str, str]，完整 system prompt, md5）。完整文本 = 研报提示词正文 + 工具说明段
        """
        body = self._load_body()
        full = body.rstrip() + "\n\n" + RESEARCH_PROTOCOL_V2 + "\n\n" + tool_docs
        return full, hashlib.md5(full.encode("utf-8")).hexdigest()


def render_tool_docs(specs: list[_ToolSpecLike]) -> str:
    """工具说明段：名称 + 描述 + 必填参数（schema 明细经 API tools 字段单独下发）。

    参数：
        specs: list[_ToolSpecLike]，合约规格映射
    返回：
        str，工具说明段：名称 + 描述 + 必填参数（schema 明细经 API tools 字段单独下发）
    """
    lines = ["## 可用工具", ""]
    for t in specs:
        required = "、".join(t.parameters.get("required", [])) or "无"
        lines.append(f"- `{t.name}`：{t.description}（必填参数：{required}）")
    return "\n".join(lines)
