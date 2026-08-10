"""system_prompt.md 加载（mtime 热重载）+ 工具说明自动拼接。

策略书可由人工或复盘 Agent 修改；每次取 system prompt 时检查文件 mtime，
变更即重新读取，保存后下一轮决策自动生效。工具说明段由注册表自动生成，
避免策略书与工具定义两份文档漂移。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from src.agent.tools import ToolSpec


EXECUTION_RESEARCH_POLICY_V2 = """## 强制策略附录：EXECUTION_RESEARCH_POLICY_V2

- 研报仅按当前下单合约参考，禁止把 BTC_USDT 的结论套用到其他合约。
- technical_confirmation=冲突 或 不可用时优先等待确认，不得把研报当成下单指令。
- basis_type=结构延续 属于软参考，即使方向明确也不能当成风控硬闸门或自动开仓理由。
- 研报提供方向背景，不替代当轮行情、入场条件、止损和代码风控。
"""


class PromptLoader:
    """策略书加载器：缓存 + mtime 检测热重载。"""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._mtime: float | None = None
        self._body: str = ""

    def _load_body(self) -> str:
        try:
            mtime = self._path.stat().st_mtime
        except FileNotFoundError:
            if self._mtime is None:
                raise
            return self._body  # 运行中被删除：沿用缓存，不中断决策
        if mtime != self._mtime:
            self._body = self._path.read_text(encoding="utf-8")
            self._mtime = mtime
        return self._body

    def system_prompt(self, tools: list[ToolSpec]) -> tuple[str, str]:
        """返回（完整 system prompt, md5）。完整文本 = 策略书 + 工具说明段。"""
        body = self._load_body()
        full = body.rstrip() + "\n\n" + EXECUTION_RESEARCH_POLICY_V2
        full += "\n\n" + render_tool_docs(tools)
        return full, hashlib.md5(full.encode("utf-8")).hexdigest()

    def body_md5(self) -> str:
        """策略书原文（不含工具说明段）的 md5，作为决策/成交与策略版本的关联键。"""
        return hashlib.md5(self._load_body().encode("utf-8")).hexdigest()


def render_tool_docs(tools: list[ToolSpec]) -> str:
    """工具说明段：名称 + 描述 + 必填参数（schema 明细经 API tools 字段单独下发）。"""
    lines = ["## 可用工具", ""]
    for t in tools:
        required = "、".join(t.parameters.get("required", [])) or "无"
        lines.append(f"- `{t.name}`：{t.description}（必填参数：{required}）")
    return "\n".join(lines)
