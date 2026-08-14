"""研报超时审计：在取消原因链中回收已经收到的模型响应。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Any

from src.audit.trail import AuditTrail


async def record_failed_raw(
    audit: AuditTrail,
    round_id: str,
    raw_parts: list[str],
    exc: BaseException,
) -> None:
    """从异常原因链提取模型响应并补写当前审计轮。

    参数：
        audit: AuditTrail，研报轮使用的审计写入入口
        round_id: str，关联的审计轮次编号
        raw_parts: list[str]，累计保存模型响应审计流的列表
        exc: BaseException，可能自身或原因链携带 raw 属性的异常

    返回：
        None，找到未记录的响应时追加并实时写审计；无内容时不写
    """
    current: BaseException | None = exc
    while current is not None:
        if failed_raw := getattr(current, "raw", ""):
            if not raw_parts or raw_parts[-1] != failed_raw:
                raw_parts.append(failed_raw)
                await audit.record_llm_raw(round_id, "\n".join(raw_parts))
            return
        current = current.__cause__ or current.__context__


async def wait_with_raw(
    operation: Awaitable[Any],
    timeout: float,
    audit: AuditTrail,
    round_id: str,
    raw_parts: list[str],
) -> Any:
    """执行受限时操作，并在超时取消时回收原因链中的模型响应。

    参数：
        operation: Awaitable[Any]，需要受研报保险丝约束的异步操作
        timeout: float，允许操作占用的最长秒数
        audit: AuditTrail，研报轮使用的审计写入入口
        round_id: str，关联的审计轮次编号
        raw_parts: list[str]，累计保存模型响应审计流的列表

    返回：
        Any，异步操作在超时前正常产生的结果

    异常：
        TimeoutError: 操作超时，已先补写原因链中的模型响应
        asyncio.CancelledError: 外部取消操作，已先补写内层任务携带的模型响应
    """
    cancelled_raw: list[str] = []

    async def capture_cancelled_raw() -> Any:
        """执行目标操作，并在取消离开内层任务前暂存其模型响应。

        参数：无

        返回：
            Any，目标操作正常完成时产生的结果

        异常：
            asyncio.CancelledError: 保存异常携带的 raw 后原样传播取消
        """
        try:
            return await operation
        except asyncio.CancelledError as exc:
            if raw := getattr(exc, "raw", ""):
                cancelled_raw.append(raw)
            raise

    try:
        return await asyncio.wait_for(capture_cancelled_raw(), timeout=timeout)
    except TimeoutError as exc:
        await record_failed_raw(audit, round_id, raw_parts, exc)
        raise
    except asyncio.CancelledError as exc:
        if cancelled_raw:
            setattr(exc, "raw", cancelled_raw[-1])
            await record_failed_raw(audit, round_id, raw_parts, exc)
        raise
