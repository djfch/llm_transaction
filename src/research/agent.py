"""研报 agent：多轮工具调用循环，最终文本须为结构化 JSON。

不变量：
- provider 为 None（LLM 未配置）→ 直接返回失败，不落审计、不落研报；
- 正常路径：wake_source='research' 开审计轮 → 预注入组装（五段数据）作第一轮
  user 消息 → ≤max_turns 工具循环（整体超时强制终止）→ 最终文本解析 JSON
  （direction/confidence 必填）→ 成功落 research_reports → 回填落库本轮暂存的
  因果链（submit_causal_links 只暂存，LLM 无需预知研报 id）；解析失败重试 1 次，
  仍失败落 error 报告，暂存链随 deps 丢弃；
- WS 事件（notify_event 注入时）：begin_round 后 research_round_start、审计轮结束
  research_round（ok 随成败，_fail 路径 ok=False）；事件失败只记日志；provider None 早退零事件；
- chat loop 任何异常：落 error 报告 + 审计轮 error + 返回 {'ok': False}，
  绝不向上抛，确保研报失败不影响交易决策循环；
- 本模块不 import src/agent/*：provider 以结构化鸭子类型注入（与
  src.agent.providers.base.LLMProvider 协议一致，生产由 bootstrap 复用同一实例）。
"""

from __future__ import annotations

import asyncio
import time
import json
from collections.abc import Callable
from typing import Any, Protocol

from src.audit.logger import get_logger
from src.audit.trail import AuditTrail
from src.config import Settings
from src.memory.repo import Repo
from src.research.payload import _parse_payload
from src.research.preinject import build_preinjection
from src.research.prompts import ResearchPromptLoader, render_tool_docs
from src.research.tool_handlers import ResearchToolDeps
from src.research.tools import ResearchToolRegistry
from src.utils import maybe_await

logger = get_logger(__name__)


class _ProviderProtocol(Protocol):
    """研报 agent 依赖的 LLM 接口（结构化鸭子类型，不 import src/agent/*）。

    与 src.agent.providers.base.LLMProvider 协议一致；响应对象需有
    text/tool_calls/raw/assistant_message 属性，调用对象需有 name/args 属性。
    """

    async def chat(self, system: str, messages: list[dict], tools: list[dict]) -> Any: ...

    def tool_result_message(self, call: Any, result: str) -> dict: ...


class ResearchAgent:
    """研报 agent。所有依赖构造期注入，provider 可空、可热替换。"""

    def __init__(
        self,
        *,
        settings: Settings,
        provider: _ProviderProtocol | None,
        repo: Repo,
        audit: AuditTrail,
        prompt_loader: ResearchPromptLoader,
        data_provider: Any,  # ResearchDataProvider（鸭子类型，防循环 import）
        notify_event: Callable[[dict], None] | None = None,  # WS 事件广播（轮始/轮末）
        max_turns: int = 30,
        timeout_seconds: int = 900,
    ) -> None:
        self._settings = settings
        self._provider = provider
        self._repo = repo
        self._audit = audit
        self._prompts = prompt_loader
        self._data_provider = data_provider
        self._notify_event = notify_event  # None 则不广播（测试/未接线场景零事件）
        self._max_turns = max(1, max_turns)
        self._timeout = max(60, timeout_seconds)

    def set_provider(self, provider: _ProviderProtocol) -> None:
        """热替换 LLM provider（与 DecisionLoop/ReviewAgent 同模式）。"""
        self._provider = provider

    @property
    def llm_configured(self) -> bool:
        return self._provider is not None

    async def run(self, report_type: str = "manual", hours: int = 24) -> dict:
        """执行一次研报。失败返回 {'ok': False, 'error': ...}，绝不向上抛。"""
        if self._provider is None:
            logger.warning("LLM 未配置，跳过本次研报")
            return {"ok": False, "error": "LLM 未配置", "error_code": "llm_not_configured"}
        if not 1 <= hours <= 48:  # L10：与工具层同口径
            return {"ok": False, "error": f"参数错误：hours 须在 1-48 之间（当前 {hours}）"}
        deps = ResearchToolDeps(
            provider=self._data_provider,
            repo=self._repo,
            mode=self._settings.mode,
        )
        registry = ResearchToolRegistry(deps)
        raw_parts: list[str] = []
        round_id = ""
        try:
            full_prompt, _ = self._prompts.system_prompt(render_tool_docs(registry.specs))
            round_id = await self._audit.begin_round(self._settings.mode, "research", full_prompt)
            await self._emit_event({"type": "research_round_start", "data": {"round_id": round_id}})
            briefing = await build_preinjection(deps, hours)
            await self._audit.record_context(round_id, briefing)
            text = await self._chat_with_timeout(
                full_prompt, briefing, registry, round_id, raw_parts
            )
            payload = await self._parse_json_with_retry(
                full_prompt, briefing, registry, round_id, raw_parts, text
            )
            report = await self._repo.research.save_report(
                report_type=report_type,
                direction=payload["direction"],
                confidence=payload["confidence"],
                horizon=payload.get("horizon", ""),
                evidence_json=json.dumps(payload.get("evidence", []), ensure_ascii=False),
                risks_json=json.dumps(payload.get("risks", []), ensure_ascii=False),
                narrative=payload.get("narrative", ""),
                raw_json=json.dumps(payload, ensure_ascii=False),
                round_id=round_id,
            )
            links_saved = await self._flush_causal_links(deps, report.id)
            await self._audit.end_round(round_id, "\n".join(raw_parts))
            await self._emit_event(
                {"type": "research_round", "data": {"round_id": round_id, "ok": True}}
            )
            logger.info(
                "研报完成 report_id=%s type=%s %s/%s 因果链=%d",
                report.id,
                report_type,
                report.direction,
                report.confidence,
                links_saved,
            )
            return {
                "ok": True,
                "report_id": report.id,
                "round_id": round_id,
                "direction": report.direction,
                "confidence": report.confidence,
            }
        except asyncio.CancelledError:
            # 外部取消：落 error 报告收尾审计后重新抛出（保持 asyncio 取消语义，M6）
            await self._fail(round_id, raw_parts, report_type, asyncio.CancelledError("研报被取消"))
            raise
        except Exception as e:
            return await self._fail(round_id, raw_parts, report_type, e)

    async def _flush_causal_links(self, deps: ResearchToolDeps, report_id: int) -> int:
        """把本轮暂存的因果链回填 report_id 批量落库（H1：LLM 无需预知 id）。

        单条落库失败只记日志、不影响研报主产物；返回成功条数。
        """
        saved = 0
        for link in deps.pending_causal_links:
            try:
                await self._repo.research.save_causal_link(report_id=report_id, **link)
                saved += 1
            except Exception:
                logger.exception("因果链落库失败（report_id=%s，跳过该条）", report_id)
        return saved

    async def _chat_with_timeout(
        self,
        prompt: str,
        briefing: str,
        registry: ResearchToolRegistry,
        round_id: str,
        raw_parts: list[str],
    ) -> str:
        """整体超时强制终止（保险丝：防 LLM/工具卡死无限烧钱）。"""
        return await asyncio.wait_for(
            self._chat_loop(prompt, briefing, registry, round_id, raw_parts),
            timeout=self._timeout,
        )

    async def _chat_loop(
        self,
        prompt: str,
        briefing: str,
        registry: ResearchToolRegistry,
        round_id: str,
        raw_parts: list[str],
    ) -> str:
        """多轮对话：LLM 返回工具调用就执行并回填，直到无调用或达轮次上限。"""
        messages: list[dict] = [{"role": "user", "content": briefing}]
        schemas = registry.schemas()
        text, seq = "", 0
        for _ in range(self._max_turns):
            resp = await self._provider.chat(prompt, messages, schemas)  # type: ignore[union-attr]
            raw_parts.append(resp.raw)
            if resp.assistant_message is not None:
                messages.append(resp.assistant_message)
            text = resp.text
            if not resp.tool_calls:
                return text
            for call in resp.tool_calls:
                seq += 1
                started = time.monotonic()
                result = await registry.execute(call.name, call.args)
                duration_ms = int((time.monotonic() - started) * 1000)
                await self._audit.record_tool_call(
                    round_id,
                    seq,
                    call.name,
                    args=json.dumps(call.args, ensure_ascii=False, default=str),
                    risk_verdict="",
                    risk_reason="",
                    result=json.dumps({"text": result}, ensure_ascii=False),
                    duration_ms=duration_ms,
                )
                messages.append(self._provider.tool_result_message(call, result))  # type: ignore[union-attr]
        logger.warning("round=%s 研报达到最大工具轮次 %d，强制结束", round_id[:8], self._max_turns)
        return text

    async def _parse_json_with_retry(
        self,
        prompt: str,
        briefing: str,
        registry: ResearchToolRegistry,
        round_id: str,
        raw_parts: list[str],
        text: str,
    ) -> dict:
        """解析最终 JSON；失败反馈给 LLM 重试 1 次，仍失败抛错（落 error 报告）。"""
        for attempt in range(2):
            payload = _parse_payload(text)
            if payload is not None:
                return payload
            if attempt == 0:
                messages = [
                    {"role": "user", "content": briefing},
                    {"role": "assistant", "content": text},
                    {
                        "role": "user",
                        "content": (
                            "输出不是合法研报 JSON（需含 direction/confidence，取值见要求）。"
                            "请只输出修正后的 JSON，不要解释。"
                        ),
                    },
                ]
                resp = await self._provider.chat(prompt, messages, registry.schemas())  # type: ignore[union-attr]
                raw_parts.append(resp.raw)
                if resp.assistant_message is not None:
                    messages.append(resp.assistant_message)
                if resp.tool_calls:  # L7：重试响应若带工具调用则执行回填，再取最终文本
                    seq = 900  # 高位基数，避免与主循环 seq 冲突
                    for call in resp.tool_calls:
                        seq += 1
                        result = await registry.execute(call.name, call.args)
                        await self._audit.record_tool_call(
                            round_id,
                            seq,
                            call.name,
                            args=json.dumps(call.args, ensure_ascii=False, default=str),
                            risk_verdict="",
                            risk_reason="",
                            result=json.dumps({"text": result}, ensure_ascii=False),
                            duration_ms=0,
                        )
                        messages.append(self._provider.tool_result_message(call, result))  # type: ignore[union-attr]
                    resp = await self._provider.chat(prompt, messages, registry.schemas())  # type: ignore[union-attr]
                    raw_parts.append(resp.raw)
                text = resp.text
        raise ValueError("研报输出解析失败：非合法 JSON 或缺必填字段")

    async def _fail(
        self, round_id: str, raw_parts: list[str], report_type: str, exc: Exception
    ) -> dict:
        """失败收尾：落 error 报告 + 审计轮 error，绝不向上抛（不变量⑤）。

        round_id 为空（begin_round 前失败）时跳过审计结束；落库/审计自身失败
        只记日志，不把失败升级为异常。
        """
        error = f"{type(exc).__name__}: {exc}"
        logger.exception("研报失败：%s", error)
        try:
            await self._repo.research.save_report(
                report_type=report_type,
                direction="中性",
                confidence="低",
                error=error,
                round_id=round_id,
            )
        except Exception:
            logger.exception("研报失败报告落库失败（继续返回失败结果）")
        if round_id:
            try:
                await self._audit.end_round(round_id, "\n".join(raw_parts), error=error)
            except Exception:
                logger.exception("研报审计轮结束失败（继续返回失败结果）")
            await self._emit_event(
                {"type": "research_round", "data": {"round_id": round_id, "ok": False}}
            )
        return {"ok": False, "error": error, "round_id": round_id}

    async def _emit_event(self, payload: dict) -> None:
        """广播 WS 事件：回调可同步/异步（与复盘同容错模式）；失败只记日志。"""
        if self._notify_event:
            try:
                await maybe_await(self._notify_event(payload))
            except Exception:
                logger.exception("研报事件广播失败")
