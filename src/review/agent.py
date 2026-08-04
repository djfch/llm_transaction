"""复盘 agent：多轮工具调用循环，最终文本即复盘报告。

不变量：
- provider 为 None（LLM 未配置）→ 直接返回失败，不落审计、不落报告；
- 正常路径：wake_source='review' 开审计轮 → 中文简报（区间 + 当前策略全文 +
  代码侧预统计 + 引导语）→ ≤max_turns 工具循环 → 最终文本落 review_reports →
  有修订则版本↔报告互相关联（策略书与指标短名单各自判空关联）→ 结束审计轮 → on_alert 摘要（html.escape 且 ≤500 字符）；
- chat loop 任何异常：落 error 报告 + 审计轮 error + 失败告警，返回 {'ok': False}，
  绝不向上抛，确保复盘失败不影响交易决策循环；
- 本模块不 import src/agent/*：provider 以结构化鸭子类型注入（与
  src.agent.providers.base.LLMProvider 协议一致，生产由 bootstrap 复用同一实例）。
"""

from __future__ import annotations

import html
import json
import time
from collections.abc import Awaitable, Callable, Iterable
from typing import Any, Protocol

from src.audit.logger import get_logger
from src.audit.trail import AuditTrail
from src.config import Settings
from src.market.indicator_service import IndicatorService
from src.memory.repo import Repo
from src.review.indicator_config import IndicatorConfigStore
from src.review.prompts import ReviewPromptLoader, render_tool_docs
from src.review.stats import compute_review_stats, format_stats_text
from src.review.strategy import StrategyStore
from src.review.tool_handlers import ReviewToolDeps
from src.review.tools import ReviewToolRegistry
from src.utils import maybe_await

logger = get_logger(__name__)

AlertCallback = Callable[[str], Awaitable[None] | None]

_ALERT_LIMIT = 500  # 通知摘要长度上限


class _ProviderProtocol(Protocol):
    """复盘 agent 依赖的 LLM 接口（结构化鸭子类型，不 import src/agent/*）。

    与 src.agent.providers.base.LLMProvider 协议一致；响应对象需有
    text/tool_calls/raw/assistant_message 属性，调用对象需有 name/args 属性。
    """

    async def chat(self, system: str, messages: list[dict], tools: list[dict]) -> Any: ...

    def tool_result_message(self, call: Any, result: str) -> dict: ...


def _fmt_time(ts: float) -> str:
    """Unix 秒 → 本地时间字符串。"""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def _escape_alert(text: str, limit: int = _ALERT_LIMIT) -> str:
    """通知摘要安全处理：html.escape 后 ≤limit 字符，防止 HTML 注入。

    超长时在转义后文本上截断，并回退到完整 HTML 实体边界（避免截出半个 &amp;）。
    """
    escaped = html.escape(text)
    if len(escaped) <= limit:
        return escaped
    cut = escaped[: limit - 3]
    if cut.rfind("&") > cut.rfind(";"):
        cut = cut[: cut.rfind("&")]
    return cut + "..."


class ReviewAgent:
    """复盘 agent。所有依赖构造期注入，provider 可空、可热替换。"""

    def __init__(
        self,
        *,
        settings: Settings,
        provider: _ProviderProtocol | None,
        repo: Repo,
        audit: AuditTrail,
        store: StrategyStore,
        prompt_loader: ReviewPromptLoader,
        on_alert: AlertCallback | None = None,
        max_turns: int = 12,
        indicator_service: IndicatorService | None = None,
        indicator_config_store: IndicatorConfigStore | None = None,
        watchlist: Iterable[str] | None = None,
    ) -> None:
        self._settings = settings
        self._provider = provider
        self._repo = repo
        self._audit = audit
        self._store = store
        self._prompts = prompt_loader
        self._on_alert = on_alert
        self._max_turns = max(1, max_turns)
        # 指标三件套默认 None/空（未装配）：指标工具降级为中文提示，其余工具不受影响
        self._indicator_service = indicator_service
        self._indicator_config_store = indicator_config_store
        # watchlist 保留活引用（装配传入与执行 agent 共享的同一 list，前端改名单原地生效），
        # 每轮 run 构造 deps 时才拍快照，避免固化启动时名单（热更新后复盘看不到新合约）
        self._watchlist = watchlist

    def set_provider(self, provider: _ProviderProtocol) -> None:
        """热替换 LLM provider（与 DecisionLoop 同模式，配置重建后即生效）。"""
        self._provider = provider

    async def run(self, period_start: float, period_end: float) -> dict:
        """执行一次复盘。失败返回 {'ok': False, 'error': ...}，绝不向上抛。"""
        if self._provider is None:
            logger.warning("LLM 未配置，跳过本次复盘")
            return {"ok": False, "error": "LLM 未配置", "error_code": "llm_not_configured"}
        # 每次 run 新建 deps/registry（轻量）：created_version_id 不复用、不串场
        deps = ReviewToolDeps(
            repo=self._repo,
            store=self._store,
            mode=self._settings.mode,
            indicator_service=self._indicator_service,
            indicator_config_store=self._indicator_config_store,
            watchlist=tuple(self._watchlist or ()),  # 每轮对活名单拍快照，跟随热更新
        )
        registry = ReviewToolRegistry(deps)
        full_prompt, _ = self._prompts.system_prompt(render_tool_docs(registry.specs))
        round_id = await self._audit.begin_round(self._settings.mode, "review", full_prompt)
        raw_parts: list[str] = []
        try:
            stats_text, stats_json = await self._pre_stats(period_start, period_end)
            briefing = self._build_briefing(period_start, period_end, stats_text)
            text = await self._chat_loop(full_prompt, briefing, registry, round_id, raw_parts)
            report_md = text.strip() or "（复盘未产出报告）"
            action = "rewrite" if deps.created_version_id is not None else "none"
            report = await self._repo.review.save_review_report(
                period_start,
                period_end,
                stats_json,
                report_md,
                action,
                new_version_id=deps.created_version_id,
            )
            if deps.created_version_id is not None:
                await self._repo.review.attach_report_to_version(deps.created_version_id, report.id)
            if deps.indicator_config_version_id is not None:  # 指标短名单版本同模式关联
                await self._repo.indicator_config.attach_report_to_version(
                    deps.indicator_config_version_id, report.id
                )
            await self._audit.end_round(round_id, "\n".join(raw_parts))
        except Exception as e:
            return await self._fail(round_id, raw_parts, period_start, period_end, e)
        await self._notify(_success_alert(report_md, deps.created_version_id))
        logger.info("复盘完成 report_id=%s action=%s", report.id, report.strategy_action)
        return {
            "ok": True,
            "report_id": report.id,
            "round_id": round_id,
            "strategy_action": report.strategy_action,
            "new_version_id": report.new_version_id,
        }

    async def _pre_stats(self, period_start: float, period_end: float) -> tuple[str, str]:
        """代码侧预统计：中文文本（进简报）+ JSON（落 stats_json）。"""
        trades = await self._repo.review.trades_for_review(
            period_start, period_end, self._settings.mode
        )
        stats = compute_review_stats(trades)
        return format_stats_text(stats), json.dumps(stats.to_dict(), ensure_ascii=False)

    def _build_briefing(self, period_start: float, period_end: float, stats_text: str) -> str:
        """组装中文简报 user 消息：复盘区间 + 当前策略全文 + 预统计 + 引导语。"""
        return "\n".join(
            [
                f"复盘区间：{_fmt_time(period_start)} ~ {_fmt_time(period_end)}"
                f"（{self._settings.mode} 模式）",
                "",
                "## 当前策略书全文",
                self._store.current() or "（策略书文件不存在）",
                "",
                "## 区间预统计（代码计算，以此为准，不要自行重算）",
                stats_text,
                "",
                "## 工作要求",
                "- 先用 get_review_stats 看整体概况，再对可疑轮次下钻"
                "（list_decision_rounds / get_decision_detail / get_tool_call_chain /"
                " get_round_context / list_trades）；",
                "- 结论必须引用证据（round_id、数字），统计数字以工具返回为准；",
                "- 没有实质收获不要调用 submit_strategy_revision；确需修订时提交全文与理由。",
            ]
        )

    async def _chat_loop(
        self,
        prompt: str,
        briefing: str,
        registry: ReviewToolRegistry,
        round_id: str,
        raw_parts: list[str],
    ) -> str:
        """多轮对话：LLM 返回工具调用就执行并回填，直到无调用或达轮次上限。"""
        messages: list[dict] = [{"role": "user", "content": briefing}]
        schemas = registry.schemas()
        text, seq = "", 0
        for _ in range(self._max_turns):
            resp = await self._provider.chat(prompt, messages, schemas)
            raw_parts.append(resp.raw)
            if resp.assistant_message is not None:
                messages.append(resp.assistant_message)
            text = resp.text
            if not resp.tool_calls:
                return text
            for call in resp.tool_calls:
                seq += 1
                result = await self._execute_call(registry, round_id, seq, call)
                messages.append(self._provider.tool_result_message(call, result))
        logger.warning("round=%s 复盘达到最大工具轮次 %d，强制结束", round_id[:8], self._max_turns)
        return text

    async def _execute_call(
        self, registry: ReviewToolRegistry, round_id: str, seq: int, call: Any
    ) -> str:
        """执行一次工具调用并落审计（入参/结果/耗时；复盘工具无风控判定，置空串）。"""
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
        return result

    async def _fail(
        self,
        round_id: str,
        raw_parts: list[str],
        period_start: float,
        period_end: float,
        exc: Exception,
    ) -> dict:
        """失败收尾：落 error 报告 + 审计轮 error + 失败告警（不向上抛）。"""
        error = f"{type(exc).__name__}: {exc}"
        logger.exception("复盘失败：%s", error)
        report = await self._repo.review.save_review_report(
            period_start, period_end, "{}", "", "none", error=error
        )
        await self._audit.end_round(round_id, "\n".join(raw_parts), error=error)
        await self._notify(_escape_alert(f"【复盘失败】{error}"))
        return {"ok": False, "error": error, "report_id": report.id, "round_id": round_id}

    async def _notify(self, msg: str) -> None:
        """发送告警：回调可同步/异步；告警失败只记日志，不把成功复盘改判为失败。"""
        if self._on_alert is None:
            return
        try:
            await maybe_await(self._on_alert(msg))
        except Exception:
            logger.exception("复盘告警发送失败")


def _success_alert(report_md: str, version_id: int | None) -> str:
    """成功通知摘要：结论首段 + 策略动作（html.escape 且 ≤500 字符）。"""
    first = report_md.strip().split("\n\n")[0] if report_md.strip() else "（复盘未产出报告）"
    status = f"策略已更新至 v{version_id}" if version_id is not None else "策略未调整"
    return _escape_alert(f"【复盘完成】{first}\n{status}")
