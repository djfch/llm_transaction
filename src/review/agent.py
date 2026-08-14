"""复盘 agent：多轮工具调用循环，最终文本即复盘报告。

不变量：
- provider 为 None（LLM 未配置）→ 直接返回失败，不落审计、不落报告；
- 正常路径：wake_source='review' 开审计轮 → 中文简报（区间 + 当前策略全文 +
  代码侧预统计 + 引导语）→ ≤max_turns 工具循环 → 最终文本落 review_reports →
  有修订则版本↔报告互相关联（策略书与指标短名单各自判空关联）→ 结束审计轮 → on_alert 摘要（html.escape 且 ≤500 字符）；
- WS 事件（notify_event 注入时）：begin_round 后广播 review_round_start，
  结束审计轮后广播 review_round（成功 ok=True / _fail 路径 ok=False）；
  事件失败只记日志绝不影响复盘；provider None 提前返回时零事件；
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

    async def chat(self, system: str, messages: list[dict], tools: list[dict]) -> Any:
        """发起一轮 LLM 对话，返回统一响应（文本 + 工具调用 + 原文 + 原生 assistant 消息）。

        参数：
            system: str，系统提示词全文
            messages: list[dict]，对话消息列表（厂商原生格式，多轮时含历史回填）
            tools: list[dict]，可用工具 schema 列表（中性格式 name/description/parameters）

        返回：
            Any：一轮回复对象，需有 text/tool_calls/raw/assistant_message 属性
        """
        ...

    def tool_result_message(self, call: Any, result: str) -> dict:
        """把一次工具执行结果包装成厂商原生消息，供追加进 messages 继续对话。

        参数：
            call: Any，本轮的工具调用对象，需有 name/args 属性
            result: str，工具执行结果文本

        返回：
            dict：厂商原生格式的工具结果消息
        """
        ...


def _fmt_time(ts: float) -> str:
    """把 Unix 秒时间戳格式化为服务器本地时间。

    参数：
        ts: float，待格式化的 Unix 秒时间戳

    返回：
        str，格式为 YYYY-MM-DD HH:MM 的本地时间文本
    """
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def _escape_alert(text: str, limit: int = _ALERT_LIMIT) -> str:
    """通知摘要安全处理：html.escape 后 ≤limit 字符，防止 HTML 注入。

    超长时在转义后文本上截断，并回退到完整 HTML 实体边界（避免截出半个 &amp;）。

    参数：
        text: str，待放入通知消息的原始文本
        limit: int，转义后允许保留的最大字符数

    返回：
        str，已转义 HTML 且不超过长度上限的通知摘要
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
        notify_event: Callable[[dict], None] | None = None,  # WS 事件广播（轮始/轮末）
        max_turns: int = 12,
        indicator_service: IndicatorService | None = None,
        indicator_config_store: IndicatorConfigStore | None = None,
        watchlist: Iterable[str] | None = None,
    ) -> None:
        """组装复盘 agent：全部依赖构造期注入，保存为实例属性。

        参数：
            settings: Settings，全局配置（复盘用其 mode 区分模拟/实盘）
            provider: _ProviderProtocol | None，LLM 接口实例；为 None 时 run 直接返回失败
            repo: Repo，持久化仓库（复盘报告落库、版本与报告互相关联）
            audit: AuditTrail，审计溯源（开/结束审计轮、记录每次工具调用）
            store: StrategyStore，策略书存储（读取当前策略全文进简报）
            prompt_loader: ReviewPromptLoader，复盘系统提示词加载器
            on_alert: AlertCallback | None，告警回调（可同步或异步）；省略时不发送告警
            notify_event: Callable[[dict], None] | None，WS 事件广播回调（轮始/轮末）；
                省略时不广播任何事件
            max_turns: int，单次复盘的工具调用轮次上限，内部钳到至少 1；默认 12
            indicator_service: IndicatorService | None，技术指标计算服务；
                省略时指标类工具降级为中文提示
            indicator_config_store: IndicatorConfigStore | None，指标短名单版本化存储；
                省略时指标短名单工具降级
            watchlist: Iterable[str] | None，监控合约名单，保留活引用、每轮 run 拍快照
                以跟随热更新；省略时视为空名单

        返回：
            None，仅把依赖保存为实例属性（构造期装配，无其他副作用）
        """
        self._settings = settings
        self._provider = provider
        self._repo = repo
        self._audit = audit
        self._store = store
        self._prompts = prompt_loader
        self._on_alert = on_alert
        self._notify_event = notify_event  # None 则不广播（测试/未接线场景零事件）
        self._max_turns = max(1, max_turns)
        # 指标三件套默认 None/空（未装配）：指标工具降级为中文提示，其余工具不受影响
        self._indicator_service = indicator_service
        self._indicator_config_store = indicator_config_store
        # watchlist 保留活引用（装配传入与执行 agent 共享的同一 list，前端改名单原地生效），
        # 每轮 run 构造 deps 时才拍快照，避免固化启动时名单（热更新后复盘看不到新合约）
        self._watchlist = watchlist

    def set_provider(self, provider: _ProviderProtocol) -> None:
        """热替换复盘使用的 LLM provider。

        参数：
            provider: _ProviderProtocol，配置重建后生效的新 LLM 提供者

        返回：
            None，原地替换后续复盘轮次使用的 provider
        """
        self._provider = provider

    async def run(self, period_start: float, period_end: float) -> dict:
        """执行一次完整复盘并保存报告、版本关联与审计记录。

        参数：
            period_start: float，复盘区间起始 Unix 秒时间戳
            period_end: float，复盘区间结束 Unix 秒时间戳

        返回：
            dict，成功时包含报告、审计轮和策略版本信息；失败时包含错误与失败报告编号
        """
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
        await self._emit_event({"type": "review_round_start", "data": {"round_id": round_id}})
        raw_parts: list[str] = []
        try:
            stats_text, stats_json = await self._pre_stats(period_start, period_end)
            briefing = self._build_briefing(period_start, period_end, stats_text)
            await self._audit.record_context(round_id, briefing)
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
                round_id=round_id,
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
        await self._emit_event({"type": "review_round", "data": {"round_id": round_id, "ok": True}})
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
        """计算指定复盘区间的成交统计并生成两种表示。

        参数：
            period_start: float，复盘区间起始 Unix 秒时间戳
            period_end: float，复盘区间结束 Unix 秒时间戳

        返回：
            tuple[str, str]，依次为注入简报的中文统计文本和落库的 JSON 文本
        """
        trades = await self._repo.review.trades_for_review(
            period_start, period_end, self._settings.mode
        )
        stats = compute_review_stats(trades)
        return format_stats_text(stats), json.dumps(stats.to_dict(), ensure_ascii=False)

    def _build_briefing(self, period_start: float, period_end: float, stats_text: str) -> str:
        """组装包含区间、当前策略、预统计和工作要求的复盘简报。

        参数：
            period_start: float，复盘区间起始 Unix 秒时间戳
            period_end: float，复盘区间结束 Unix 秒时间戳
            stats_text: str，代码计算且要求 LLM 直接采用的中文统计

        返回：
            str，作为首条 user 消息发送给复盘 LLM 的完整简报
        """
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
        """循环执行 LLM 对话与工具回填直至得到最终文本。

        参数：
            prompt: str，包含工具说明的复盘系统提示词
            briefing: str，本轮首条 user 简报
            registry: ReviewToolRegistry，本轮复盘工具注册表
            round_id: str，关联工具审计记录的轮次编号
            raw_parts: list[str]，就地追加每轮 LLM 原始输出的列表

        返回：
            str，最后一轮 LLM 文本；达到最大工具轮次时返回当时最新文本
        """
        messages: list[dict] = [{"role": "user", "content": briefing}]
        schemas = registry.schemas()
        text, seq = "", 0
        for _ in range(self._max_turns):
            try:
                resp = await self._provider.chat(prompt, messages, schemas)
            except Exception as exc:
                if failed_raw := getattr(exc, "raw", ""):
                    raw_parts.append(failed_raw)
                    await self._audit.record_llm_raw(round_id, "\n".join(raw_parts))
                raise
            raw_parts.append(resp.raw)
            await self._audit.record_llm_raw(round_id, "\n".join(raw_parts))
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
        """执行一次复盘工具调用并记录参数、结果与耗时。

        参数：
            registry: ReviewToolRegistry，负责分派工具调用的注册表
            round_id: str，关联审计轮次编号
            seq: int，工具调用在本轮中的顺序号
            call: Any，包含 name 和 args 的统一工具调用对象

        返回：
            str，工具注册表返回并回填给 LLM 的文本结果
        """
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
        """保存失败报告并完成审计、事件广播和告警收尾。

        参数：
            round_id: str，失败复盘的审计轮次编号
            raw_parts: list[str]，本轮已累计的 LLM 原始输出
            period_start: float，失败复盘区间起始时间戳
            period_end: float，失败复盘区间结束时间戳
            exc: Exception，触发失败收尾的原始异常

        返回：
            dict，包含 ok=False、错误文本、失败报告编号和审计轮次编号
        """
        error = f"{type(exc).__name__}: {exc}"
        logger.exception("复盘失败：%s", error)
        report = await self._repo.review.save_review_report(
            period_start, period_end, "{}", "", "none", error=error, round_id=round_id
        )
        await self._audit.end_round(round_id, "\n".join(raw_parts), error=error)
        await self._emit_event(
            {"type": "review_round", "data": {"round_id": round_id, "ok": False}}
        )
        await self._notify(_escape_alert(f"【复盘失败】{error}"))
        return {"ok": False, "error": error, "report_id": report.id, "round_id": round_id}

    async def _notify(self, msg: str) -> None:
        """通过可同步或异步回调发送复盘告警。

        参数：
            msg: str，已完成安全转义的告警文本

        返回：
            None，存在回调时发送告警；发送失败仅记录日志
        """
        if self._on_alert is None:
            return
        try:
            await maybe_await(self._on_alert(msg))
        except Exception:
            logger.exception("复盘告警发送失败")

    async def _emit_event(self, payload: dict) -> None:
        """通过可同步或异步回调广播复盘 WS 事件。

        参数：
            payload: dict，包含事件类型和轮次状态的数据载荷

        返回：
            None，存在回调时广播事件；广播失败仅记录日志
        """
        if self._notify_event is None:
            return
        try:
            await maybe_await(self._notify_event(payload))
        except Exception:
            logger.exception("复盘事件广播失败")


def _success_alert(report_md: str, version_id: int | None) -> str:
    """生成包含报告首段和策略动作的复盘成功通知。

    参数：
        report_md: str，已保存的复盘报告 Markdown 正文
        version_id: int | None，新策略版本编号；为空表示本轮未调整策略

    返回：
        str，已转义 HTML 且不超过通知长度上限的成功摘要
    """
    first = report_md.strip().split("\n\n")[0] if report_md.strip() else "（复盘未产出报告）"
    status = f"策略已更新至 v{version_id}" if version_id is not None else "策略未调整"
    return _escape_alert(f"【复盘完成】{first}\n{status}")
