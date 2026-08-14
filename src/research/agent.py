"""研报 agent：多轮工具调用循环，最终文本须为结构化 JSON。

不变量：
- provider 为 None（LLM 未配置）→ 直接返回失败，不落审计、不落研报；
- 正常路径：wake_source='research' 开审计轮 → 预注入组装（时间、白名单及六类数据）作第一轮
  user 消息 → ≤max_turns 工具循环（整体超时强制终止）→ 最终文本解析 JSON
  （白名单、市场工具与 asset_views 集合相等）→ 报告头和逐标的结论原子落库 →
  回填本轮暂存的因果链（submit_causal_links 只暂存，LLM 无需预知研报 id）；解析失败以同一
  上下文原样重发（累计 3 次输出，不回灌失败内容；重试阶段同受超时保险丝
  约束），仍失败落 error 报告，暂存链随 deps 丢弃；
- WS 事件（notify_event 注入时）：begin_round 后 research_round_start、审计轮结束
  research_round（ok 随成败，_fail 路径 ok=False）；事件失败只记日志；provider None 早退零事件；
- chat loop 任何异常：落 error 报告 + 审计轮 error + 返回 {'ok': False}，
  绝不向上抛，确保研报失败不影响交易决策循环；
- 本模块不 import src/agent/*：provider 以结构化鸭子类型注入（与
  src.agent.providers.base.LLMProvider 协议一致，生产由 bootstrap 复用同一实例）。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from typing import Any, Protocol

from src.audit.logger import get_logger
from src.audit.trail import AuditTrail
from src.config import Settings
from src.memory.repo import Repo
from src.research.payload import _parse_payload
from src.research.persist import persist_payload, success_result
from src.research.preinject import build_preinjection
from src.research.prompts import ResearchPromptLoader, render_tool_docs
from src.research.tool_handlers import ResearchToolDeps
from src.research.timeout_audit import record_failed_raw, wait_with_raw
from src.research.tools import ResearchToolRegistry
from src.utils import maybe_await

logger = get_logger(__name__)


class _ProviderProtocol(Protocol):
    """研报 agent 依赖的 LLM 接口（结构化鸭子类型，不 import src/agent/*）。

    与 src.agent.providers.base.LLMProvider 协议一致；响应对象需有
    text/tool_calls/raw/assistant_message 属性，调用对象需有 name/args 属性。
    """

    async def chat(self, system: str, messages: list[dict], tools: list[dict]) -> Any:
        """发起一轮 LLM 对话，返回含文本与工具调用的响应对象。

        参数：
            system: str，系统提示词
            messages: list[dict]，对话消息列表（用户/助手/工具结果消息按序排列）
            tools: list[dict]，本轮可用工具的 schema 列表

        返回：
            Any：LLM 响应对象，需含 text/tool_calls/raw/assistant_message 属性
        """
        ...

    def tool_result_message(self, call: Any, result: str) -> dict:
        """把一次工具执行结果包装成可回填进对话的消息。

        参数：
            call: Any，LLM 返回的工具调用对象，需含 name/args 属性
            result: str，该工具的执行结果文本

        返回：
            dict：可追加到 messages 末尾的工具结果消息
        """
        ...


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
        market_data: Any | None = None,
        watchlist: list[str] | tuple[str, ...] = (),
        notify_event: Callable[[dict], None] | None = None,  # WS 事件广播（轮始/轮末）
        max_turns: int = 30,
        timeout_seconds: int = 900,
    ) -> None:
        """注入全部依赖并初始化研报 agent。

        参数：
            settings: Settings，全局配置（审计与工具层取其中的运行模式 mode）
            provider: _ProviderProtocol | None，LLM 接口；None 表示未配置，run 时直接失败返回
            repo: Repo，持久化仓库（研报与因果链落库）
            audit: AuditTrail，审计溯源（开轮、记录上下文与工具调用、收尾）
            prompt_loader: ResearchPromptLoader，研报系统提示词加载器
            data_provider: Any，研报数据源（ResearchDataProvider 鸭子类型，防循环 import）
            market_data: Any | None，行情快照接口；None 时市场快照类工具回报不可用
            watchlist: list[str] | tuple[str, ...]，合约白名单，即本轮研报的标的集合
            notify_event: Callable[[dict], None] | None，WS 事件广播回调（轮始/轮末）；
                None 则不广播（测试/未接线场景零事件）
            max_turns: int，工具调用最大轮次，小于 1 时按 1 处理
            timeout_seconds: int，单次研报整体超时秒数（保险丝），小于 60 时按 60 处理

        返回：
            None，就地初始化实例属性
        """
        self._settings = settings
        self._provider = provider
        self._repo = repo
        self._audit = audit
        self._prompts = prompt_loader
        self._market_data = market_data
        self._watchlist = watchlist
        self._data_provider = data_provider
        self._notify_event = notify_event  # None 则不广播（测试/未接线场景零事件）
        self._max_turns = max(1, max_turns)
        self._timeout = max(60, timeout_seconds)

    def set_provider(self, provider: _ProviderProtocol) -> None:
        """热替换 LLM provider（与 DecisionLoop/ReviewAgent 同模式）。

        参数：
            provider: _ProviderProtocol，新的 LLM 提供者实例

        返回：
            None：热替换 LLM provider（与 DecisionLoop/ReviewAgent 同模式）
        """
        self._provider = provider

    @property
    def llm_configured(self) -> bool:
        """判断 LLM 是否已配置（provider 非 None）。

        参数：无

        返回：
            bool：True 表示已注入 LLM provider，可执行研报
        """
        return self._provider is not None

    async def run(self, report_type: str = "manual", hours: int = 24) -> dict:
        """执行一次研报。失败返回 {'ok': False, 'error': ...}，绝不向上抛。

        参数：
            report_type: str，研报盘口类型
            hours: int，向前回溯的小时数

        返回：
            dict：执行一次研报。失败返回 {'ok': False, 'error': ...}，绝不向上抛

        异常：
        asyncio.CancelledError：外部取消研报任务时完成失败收尾后原样抛出
        """
        if self._provider is None:
            logger.warning("LLM 未配置，跳过本次研报")
            return {"ok": False, "error": "LLM 未配置", "error_code": "llm_not_configured"}
        expected_contracts = tuple(self._watchlist)
        if not 1 <= hours <= 48:  # L10：与工具层同口径
            return {"ok": False, "error": f"参数错误：hours 须在 1-48 之间（当前 {hours}）"}
        deps = ResearchToolDeps(
            provider=self._data_provider,
            market_data=self._market_data,
            watchlist_snapshot=expected_contracts,
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
            text, ask_messages = await self._chat_with_timeout(
                full_prompt, briefing, registry, round_id, raw_parts
            )
            # JSON 重试会放大该阶段调用数，与工具循环同受超时保险丝约束
            payload = await wait_with_raw(
                self._parse_json_with_retry(
                    full_prompt,
                    ask_messages,
                    registry,
                    round_id,
                    deps,
                    expected_contracts,
                    raw_parts,
                    text,
                ),
                self._timeout,
                self._audit,
                round_id,
                raw_parts,
            )
            report, asset_count = await persist_payload(
                self._repo,
                report_type=report_type,
                payload=payload,
                round_id=round_id,
                deps=deps,
            )
            links_saved = await self._flush_causal_links(deps, report.id)
            await self._audit.end_round(round_id, "\n".join(raw_parts))
            await self._emit_event(
                {"type": "research_round", "data": {"round_id": round_id, "ok": True}}
            )
            logger.info(
                "研报完成 report_id=%s type=%s asset_count=%d 因果链=%d",
                report.id,
                report_type,
                asset_count,
                links_saved,
            )
            return success_result(report, round_id, asset_count)
        except asyncio.CancelledError:
            # 外部取消：落 error 报告收尾审计后重新抛出（保持 asyncio 取消语义，M6）
            await self._fail(round_id, raw_parts, report_type, asyncio.CancelledError("研报被取消"))
            raise
        except Exception as e:
            return await self._fail(round_id, raw_parts, report_type, e)

    async def _flush_causal_links(self, deps: ResearchToolDeps, report_id: int) -> int:
        """把本轮暂存的因果链回填 report_id 批量落库（H1：LLM 无需预知 id）。

        单条落库失败只记日志、不影响研报主产物；返回成功条数。

        参数：
            deps: ResearchToolDeps，当前模块所需的运行依赖集合
            report_id: int，研报记录编号

        返回：
            int：把本轮暂存的因果链回填 report_id 批量落库（H1：LLM 无需预知 id）
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
    ) -> tuple[str, list[dict]]:
        """整体超时强制终止（保险丝：防 LLM/工具卡死无限烧钱）。

        参数：
            prompt: str，本轮使用的系统提示词
            briefing: str，预注入的研报事实简报
            registry: ResearchToolRegistry，本轮可执行的工具注册表
            round_id: str，关联的审计轮次编号
            raw_parts: list[str]，累计保存 LLM 原始输出的列表

        返回：
            tuple[str, list[dict]]：整体超时强制终止（保险丝：防 LLM/工具卡死无限烧钱）
        """
        return await wait_with_raw(
            self._chat_loop(prompt, briefing, registry, round_id, raw_parts),
            self._timeout,
            self._audit,
            round_id,
            raw_parts,
        )

    async def _chat_loop(
        self,
        prompt: str,
        briefing: str,
        registry: ResearchToolRegistry,
        round_id: str,
        raw_parts: list[str],
    ) -> tuple[str, list[dict]]:
        """多轮对话：LLM 返回工具调用就执行并回填，直到无调用或达轮次上限。

        返回 (最终文本, 可重发前缀)：前缀即产生该文本的请求上下文（快照于
        本轮 assistant 消息回填之前），供最终 JSON 解析失败时原样重发。
        轮次耗尽时前缀为全部 messages（末尾是工具结果，可直接重发）。

        参数：
            prompt: str，本轮使用的系统提示词
            briefing: str，预注入的研报事实简报
            registry: ResearchToolRegistry，本轮可执行的工具注册表
            round_id: str，关联的审计轮次编号
            raw_parts: list[str]，累计保存 LLM 原始输出的列表

        返回：
            tuple[str, list[dict]]：多轮对话：LLM 返回工具调用就执行并回填，直到无调用或达轮次上限
        """
        messages: list[dict] = [{"role": "user", "content": briefing}]
        schemas = registry.schemas()
        text, seq = "", 0
        for _ in range(self._max_turns):
            try:
                resp = await self._provider.chat(prompt, messages, schemas)  # type: ignore[union-attr]
            except Exception as exc:
                await record_failed_raw(self._audit, round_id, raw_parts, exc)
                raise
            raw_parts.append(resp.raw)
            await self._audit.record_llm_raw(round_id, "\n".join(raw_parts))
            prefix = list(messages)  # 本轮请求所用上下文快照
            if resp.assistant_message is not None:
                messages.append(resp.assistant_message)
            text = resp.text
            if not resp.tool_calls:
                return text, prefix
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
        return text, messages

    async def _parse_json_with_retry(
        self,
        prompt: str,
        ask_messages: list[dict],
        registry: ResearchToolRegistry,
        round_id: str,
        deps: ResearchToolDeps,
        expected_contracts: tuple[str, ...],
        raw_parts: list[str],
        text: str,
    ) -> dict:
        """解析最终 JSON；失败以产生该输出的同一上下文原样重发，累计 3 次输出仍失败抛错。

        失败内容不回灌、不追加任何纠错消息（用户口径）：重试请求与产生坏输出
        的请求完全一致（含 L7 工具轮扩展后的上下文），依赖采样非确定性自愈偶发
        坏 JSON；系统性格式错误 3 次后抛 ValueError，按既有口径落 error 报告。

        参数：
            prompt: str，本轮使用的系统提示词
            ask_messages: list[dict]，产生当前输出的完整对话消息
            registry: ResearchToolRegistry，本轮可执行的工具注册表
            round_id: str，关联的审计轮次编号
            deps: ResearchToolDeps，当前模块所需的运行依赖集合
            expected_contracts: tuple[str, ...]，最终 JSON 必须覆盖的合约集合
            raw_parts: list[str]，累计保存 LLM 原始输出的列表
            text: str，待处理的文本

        返回：
            dict：解析最终 JSON；失败以产生该输出的同一上下文原样重发，累计 3 次输出仍失败抛错

        异常：
            ValueError：'研报输出解析失败：同上下文重试 3 次仍非合法 JSON 或缺必填字段' 所描述的条件发生时
        """
        for attempt in range(3):
            payload = _parse_payload(
                text,
                expected_contracts=expected_contracts,
                queried_contracts=deps.market_data_contracts,
                data_statuses={
                    contract: str(snapshot.get("data_status", "不可用"))
                    for contract, snapshot in deps.market_snapshots.items()
                },
            )
            if payload is not None:
                return payload
            if attempt < 2:
                text, ask_messages = await self._reask_final(
                    prompt,
                    ask_messages,
                    registry,
                    round_id,
                    raw_parts,
                    seq_base=900 + attempt * 100,
                )
        raise ValueError("研报输出解析失败：同上下文重试 3 次仍非合法 JSON 或缺必填字段")

    async def _reask_final(
        self,
        prompt: str,
        ask_messages: list[dict],
        registry: ResearchToolRegistry,
        round_id: str,
        raw_parts: list[str],
        seq_base: int,
    ) -> tuple[str, list[dict]]:
        """以同一上下文前缀原样重发，返回 (新一轮最终文本, 该文本的真实产生上下文)。

        无工具调用时上下文仍为原前缀（本体不就地修改）；响应携带工具调用时（L7）
        执行回填后再取一次文本，此时文本产生于"前缀 + assistant + 工具结果"的
        扩展上下文并一并返回，供下一次重发沿用。工具审计 seq 从 seq_base 起排，
        避免与主循环及其他重试轮冲突。

        参数：
            prompt: str，本轮使用的系统提示词
            ask_messages: list[dict]，产生当前输出的完整对话消息
            registry: ResearchToolRegistry，本轮可执行的工具注册表
            round_id: str，关联的审计轮次编号
            raw_parts: list[str]，累计保存 LLM 原始输出的列表
            seq_base: int，追加工具调用的序号起点

        返回：
            tuple[str, list[dict]]：以同一上下文前缀原样重发，返回 (新一轮最终文本, 该文本的真实产生上下文)
        """
        messages = list(ask_messages)
        try:
            resp = await self._provider.chat(prompt, messages, registry.schemas())  # type: ignore[union-attr]
        except Exception as exc:
            await record_failed_raw(self._audit, round_id, raw_parts, exc)
            raise
        raw_parts.append(resp.raw)
        await self._audit.record_llm_raw(round_id, "\n".join(raw_parts))
        if resp.assistant_message is not None:
            messages.append(resp.assistant_message)
        if not resp.tool_calls:
            return resp.text, ask_messages
        seq = seq_base
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
        try:
            resp = await self._provider.chat(prompt, messages, registry.schemas())  # type: ignore[union-attr]
        except Exception as exc:
            await record_failed_raw(self._audit, round_id, raw_parts, exc)
            raise
        raw_parts.append(resp.raw)
        await self._audit.record_llm_raw(round_id, "\n".join(raw_parts))
        return resp.text, messages

    async def _fail(
        self, round_id: str, raw_parts: list[str], report_type: str, exc: Exception
    ) -> dict:
        """失败收尾：落 error 报告 + 审计轮 error，绝不向上抛（不变量⑤）。

        round_id 为空（begin_round 前失败）时跳过审计结束；落库/审计自身失败
        只记日志，不把失败升级为异常。

        参数：
            round_id: str，关联的审计轮次编号
            raw_parts: list[str]，累计保存 LLM 原始输出的列表
            report_type: str，研报盘口类型
            exc: Exception，捕获到的原始异常

        返回：
            dict：失败收尾：落 error 报告 + 审计轮 error，绝不向上抛（不变量⑤）
        """
        error = f"{type(exc).__name__}: {exc}"
        logger.exception("研报失败：%s", error)
        try:
            await self._repo.research.save_failed_report(
                report_type=report_type,
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
        """广播 WS 事件：回调可同步/异步（与复盘同容错模式）；失败只记日志。

        参数：
            payload: dict，待广播、保存或转换的数据载荷

        返回：
            None：广播 WS 事件：回调可同步/异步（与复盘同容错模式）；失败只记日志
        """
        if self._notify_event:
            try:
                await maybe_await(self._notify_event(payload))
            except Exception:
                logger.exception("研报事件广播失败")
