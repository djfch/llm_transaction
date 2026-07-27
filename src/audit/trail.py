"""审计追踪：一轮决策的完整溯源（SQLite + JSON 快照双写）。

每轮决策生成 round_id，记录 prompt 快照（含 md5）、上下文快照、LLM 原始输出、
工具调用链（工具名/入参/风控判定/执行结果/耗时）与异常信息。
SQLite 供列表与详情查询，logs/audit/round_<id>.json 全文快照供回放与前端钻取。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

from src.audit.logger import get_logger
from src.config import AuditConfig
from src.memory.repo import Repo

logger = get_logger(__name__)


def _to_json_str(value: Any) -> str:
    """入参/结果统一序列化为 JSON 字符串；已是字符串则原样保留。"""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


class AuditTrail:
    """一轮决策的审计写入入口：begin → (record_context) → record_tool_call × N → end。"""

    def __init__(self, repo: Repo, config: AuditConfig) -> None:
        self._repo = repo
        self._dir = Path(config.dir)

    async def begin_round(
        self,
        mode: str,
        wake_source: str,
        system_prompt: str,
        context: str = "",
        strategy_md5: str = "",
    ) -> str:
        """开启一轮：生成 round_id，存 prompt md5 与全文快照，返回 round_id。

        context 允许为空：决策循环先落审计行（保证后续任何失败都有痕迹），
        上下文构建完成后经 record_context 回填快照。
        strategy_md5 为策略书原文 md5（区别于 prompt_md5 的拼装 md5），
        供复盘按策略版本关联本轮。
        """
        round_id = uuid.uuid4().hex
        prompt_md5 = hashlib.md5(system_prompt.encode("utf-8")).hexdigest()
        await self._repo.start_audit_round(
            round_id=round_id,
            mode=mode,
            wake_source=wake_source,
            prompt_md5=prompt_md5,
            prompt_snapshot=system_prompt,
            context_snapshot=context,
            strategy_md5=strategy_md5,
        )
        logger.info("审计轮次开始 round_id=%s mode=%s wake=%s", round_id, mode, wake_source)
        return round_id

    async def record_context(self, round_id: str, context: str) -> None:
        """上下文构建完成后回填快照（配合 begin_round 的空快照先行落库）。"""
        await self._repo.update_audit_context(round_id, context)

    async def record_tool_call(
        self,
        round_id: str,
        seq: int,
        tool: str,
        args: Any,
        risk_verdict: str,
        risk_reason: str,
        result: Any,
        duration_ms: int,
    ) -> None:
        """记录一次工具调用（含风控判定与耗时）。args/result 接受 dict 或 JSON 字符串。"""
        await self._repo.save_audit_tool_call(
            round_id=round_id,
            seq=seq,
            tool=tool,
            args_json=_to_json_str(args),
            risk_verdict=risk_verdict,
            risk_reason=risk_reason,
            result_json=_to_json_str(result),
            duration_ms=duration_ms,
        )

    async def end_round(self, round_id: str, llm_raw: str, error: str = "") -> None:
        """结束一轮：补全 LLM 原始输出与异常信息，并写 JSON 全文快照。"""
        await self._repo.finish_audit_round(round_id, llm_raw=llm_raw, error=error)
        await self._write_snapshot(round_id)
        logger.info("审计轮次结束 round_id=%s error=%s", round_id, error or "无")

    async def get_round(self, round_id: str) -> dict[str, Any] | None:
        """读取一轮完整记录（主表 + 工具调用链）；不存在返回 None。供 server 层复用。"""
        round_row = await self._repo.get_audit_round(round_id)
        if round_row is None:
            return None
        calls = await self._repo.list_audit_tool_calls(round_id)
        return {
            "round": round_row.model_dump(),
            "tool_calls": [c.model_dump() for c in calls],
        }

    async def _write_snapshot(self, round_id: str) -> None:
        """把本轮完整记录写成 logs/audit/round_<round_id>.json（目录自动创建）。"""
        data = await self.get_round(round_id)
        if data is None:
            logger.warning("快照跳过：round_id=%s 不存在", round_id)
            return
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._dir / f"round_{round_id}.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
