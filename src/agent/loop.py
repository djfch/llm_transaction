"""决策循环：组装上下文 → 调 LLM → 逐个执行工具调用 → 审计归档。

不变量：
- 每轮经 AuditTrail 生成 round_id；审计行（含 prompt 快照）先于上下文构建落库，
  保证任何失败都在 audit_rounds 留痕迹；上下文构建成功后回填快照
- 每次工具调用都落审计（入参/风控判定/结果/耗时），轮结束写 JSON 全文快照
- LLM 调用或输出解析失败 → 本轮不再执行任何工具调用（不交易），失败落审计
- 每轮结束（含失败轮）经 drain_fills 把 paper 网关成交落 trades 表；
  真实网关无此钩子，由工具层下单时直接落库（见 tool_trading.save_fills_inline）
- 连续失败达 LLMConfig.max_consecutive_failures → 风控锁（kill_switch）：
  内存置位 + 经注入回调写回 config.yaml + 告警一次（不重复骚扰）
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from src.agent.context import AgentContext, ContextBuilder
from src.agent.manual_cancel import execute_manual_cancel
from src.agent.manual_close import execute_manual_close, persist_fills
from src.agent.prompts import PromptLoader
from src.agent.providers.base import LLMProvider, ToolCall
from src.agent.tool_handlers import DailyStatsFn, ToolDeps, ToolOutcome
from src.agent.tools import ToolRegistry
from src.audit.logger import get_logger
from src.audit.trail import AuditTrail
from src.config import Settings
from src.gateway.base import Gateway
from src.market.candles import CandleCache
from src.market.triggers import TriggerManager
from src.memory.repo import Repo
from src.risk.engine import RiskEngine
from src.risk.models import DailyStats
from src.utils import maybe_await

logger = get_logger(__name__)

AlertCallback = Callable[[str], Awaitable[None] | None]


@dataclass
class RoundResult:
    """一轮决策的结果摘要（供调度器/监控层使用）。"""

    round_id: str
    ok: bool
    wake_source: str
    tool_calls: int = 0
    text: str = ""
    error: str = ""


async def default_daily_stats(repo: Repo, mode: str) -> DailyStats:
    """当日统计：已实现盈亏与开仓单数均按 mode 过滤（自然日，本地时区）。"""
    now = time.localtime()
    day_start = time.mktime((now.tm_year, now.tm_mon, now.tm_mday, 0, 0, 0, 0, 0, -1))
    return await repo.daily_stats(mode, day_start)


class DecisionLoop:
    """LLM 自主决策循环。所有依赖构造期注入，provider 可空、可热替换。

    provider 为 None（LLM 未配置，如缺 API key）时 run_once 跳过本轮：
    不落审计、不计连续失败；配置补齐后经 set_provider 热替换即恢复决策。

    可选依赖：
    - drain_fills：paper 网关成交缓冲泄放钩子（PaperGateway.drain_fills）；
      为 None（真实网关）时工具层下单直接落 trades（save_fills_inline）
    - persist_kill_switch：风控锁写回 config.yaml 的回调（保持 agent 层不碰 config_io）
    - audit：共享 AuditTrail；缺省时按 settings.audit 自建（快照目录一致）
    """

    def __init__(
        self,
        *,
        settings: Settings,
        watchlist: list[str],
        provider: LLMProvider | None,
        gateway: Gateway,
        risk_engine: RiskEngine,
        repo: Repo,
        candles: CandleCache,
        triggers: TriggerManager,
        prompt_loader: PromptLoader,
        set_next_wake: Callable[[int], int] | None = None,
        on_alert: AlertCallback | None = None,
        daily_stats_fn: DailyStatsFn | None = None,
        drain_fills: Callable[[], list] | None = None,
        persist_kill_switch: Callable[[bool], None] | None = None,
        notify_event: Callable[[dict], None] | None = None,
        audit: AuditTrail | None = None,
        max_turns: int = 8,
    ) -> None:
        self._settings = settings
        self._provider = provider
        self._repo = repo
        self._prompts = prompt_loader
        self._on_alert = on_alert
        self._max_turns = max(1, max_turns)
        self._consecutive_failures = 0
        self._risk_locked = False
        self._drain_fills = drain_fills
        self._persist_fills_lock = asyncio.Lock()  # drain 与 manual_close 的落库临界区
        self._persist_kill_switch = persist_kill_switch
        self._audit = audit or AuditTrail(repo, settings.audit)
        self._deps = ToolDeps(
            gateway=gateway,
            risk_engine=risk_engine,
            risk_config=settings.risk,
            watchlist=watchlist,
            repo=repo,
            candles=candles,
            triggers=triggers,
            daily_stats_fn=daily_stats_fn or (lambda: default_daily_stats(repo, settings.mode)),
            mode=settings.mode,
            set_next_wake=set_next_wake,
            notify_event=notify_event,
            save_fills_inline=drain_fills is None,
        )
        self._registry = ToolRegistry(self._deps)
        self._context = ContextBuilder(gateway, repo, candles, triggers, watchlist)

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    @property
    def risk_locked(self) -> bool:
        return self._risk_locked

    @property
    def llm_configured(self) -> bool:
        """provider 是否已配置（status_provider 透出 /api/status 用）。"""
        return self._provider is not None

    def set_provider(self, provider: LLMProvider) -> None:
        """热替换 LLM provider（配置前端化：改 key/模型重建后下轮决策即生效）。"""
        self._provider = provider

    async def run_once(self, wake_source: str) -> RoundResult:
        """执行一轮决策。失败不向上抛；每轮结束（含失败轮）统一 drain 成交落库。"""
        if self._provider is None:
            # LLM 未配置（缺 key）：跳过本轮，不落审计、不计连续失败；
            # 但先泄放成交缓冲——paper 强平/挂单成交与 LLM 无关，跳轮不得滞留丢失
            await self._drain_round_fills("")
            logger.warning("LLM 未配置，跳过本轮决策")
            return RoundResult(round_id="", ok=False, wake_source=wake_source, error="LLM 未配置")
        prompt, prompt_md5 = self._prompts.system_prompt(self._registry.specs)
        strategy_md5 = self._prompts.body_md5()  # 策略书原文 md5，落库供复盘按版本关联
        # 先落审计行（空上下文快照）：后续任何失败都在 audit_rounds 留痕迹
        round_id = await self._audit.begin_round(
            self._settings.mode, wake_source, prompt, strategy_md5=strategy_md5
        )
        self._deps.round_id = round_id
        ctx: AgentContext | None = None
        try:
            ctx = await self._context.build(wake_source)
            await self._audit.record_context(round_id, ctx.text)
            text, raw, n_calls = await self._chat_loop(prompt, ctx, round_id)
        except Exception as e:
            result = await self._fail_round(round_id, prompt_md5, strategy_md5, wake_source, ctx, e)
        else:
            self._consecutive_failures = 0
            await self._repo.save_decision(
                round_id=round_id,
                mode=self._settings.mode,
                strategy_version=prompt_md5,
                strategy_md5=strategy_md5,
                wake_source=wake_source,
                context_summary=ctx.summary,
                llm_raw=raw,
            )
            await self._audit.end_round(round_id, raw)
            result = RoundResult(
                round_id=round_id,
                ok=True,
                wake_source=wake_source,
                tool_calls=n_calls,
                text=text,
            )
        await self._drain_round_fills(round_id)
        return result

    async def _chat_loop(
        self, prompt: str, ctx: AgentContext, round_id: str
    ) -> tuple[str, str, int]:
        """多轮对话：LLM 返回工具调用就执行并回填结果，直到无调用或达轮次上限。"""
        messages: list[dict] = [{"role": "user", "content": ctx.text}]
        schemas = self._registry.schemas()
        raw_parts: list[str] = []
        text, total = "", 0
        for _ in range(self._max_turns):
            resp = await self._provider.chat(prompt, messages, schemas)
            raw_parts.append(resp.raw)
            if resp.assistant_message is not None:
                messages.append(resp.assistant_message)
            text = resp.text
            if not resp.tool_calls:
                return text, "\n".join(raw_parts), total
            for call in resp.tool_calls:
                outcome = await self._execute_call(round_id, total + 1, call)
                total += 1
                messages.append(self._provider.tool_result_message(call, outcome.text))
        logger.warning("round=%s 达到最大工具轮次 %d，强制结束", round_id[:8], self._max_turns)
        return text, "\n".join(raw_parts), total

    async def _execute_call(self, round_id: str, seq: int, call: ToolCall) -> ToolOutcome:
        """执行一次工具调用并落审计（入参/风控判定/结果/耗时）。"""
        started = time.monotonic()
        outcome = await self._registry.execute(call.name, call.args)
        duration_ms = int((time.monotonic() - started) * 1000)
        await self._audit.record_tool_call(
            round_id,
            seq,
            call.name,
            args=json.dumps(call.args, ensure_ascii=False, default=str),
            risk_verdict=outcome.risk_verdict,
            risk_reason=outcome.risk_reason,
            result=json.dumps({"text": outcome.text}, ensure_ascii=False),
            duration_ms=duration_ms,
        )
        return outcome

    async def _fail_round(
        self,
        round_id: str,
        prompt_md5: str,
        strategy_md5: str,
        wake_source: str,
        ctx: AgentContext | None,
        exc: Exception,
    ) -> RoundResult:
        """失败收尾：计数、落决策与审计（error 字段），达标则加锁告警。

        ctx 为 None 表示上下文构建阶段即失败（无上下文摘要可落）。
        """
        self._consecutive_failures += 1
        error = f"{type(exc).__name__}: {exc}"
        logger.warning(
            "round=%s LLM 调用失败（连续 %d 次）：%s",
            round_id[:8],
            self._consecutive_failures,
            error,
        )
        await self._repo.save_decision(
            round_id=round_id,
            mode=self._settings.mode,
            strategy_version=prompt_md5,
            strategy_md5=strategy_md5,
            wake_source=wake_source,
            context_summary=ctx.summary if ctx else "",
            llm_raw="",
        )
        await self._audit.end_round(round_id, "", error=error)
        if self._consecutive_failures >= self._settings.llm.max_consecutive_failures:
            await self._engage_lock()
        return RoundResult(round_id=round_id, ok=False, wake_source=wake_source, error=error)

    async def _drain_round_fills(self, round_id: str) -> None:
        """paper 网关成交缓冲落 trades 表（真实网关无此钩子，由工具层直接落库）。

        与 manual_close 共用 _persist_fills_lock：用户平仓直接消费缓冲落库，
        轮末 drain 不会重复落库（双计防护见 manual_close.execute_manual_close）。
        source 标注：强平 → liquidation，fill.is_close → llm_close，其余 → llm_open。
        """
        if self._drain_fills is None:
            return
        async with self._persist_fills_lock:
            await persist_fills(self._repo, self._settings.mode, round_id, self._drain_fills())

    async def manual_close(self, contract: str) -> dict:
        """用户手动平仓（监控界面）：与 LLM 平仓同一风控路径，成交标注 source=user_close。

        - 风控拒绝抛 ManualCloseRiskDenied（消息为风控理由，server 层映射 HTTP 422）；
          网关错误（如合约不存在）以 GatewayError 原样上抛
        - 决策轮进行中也可调用（用户操作优先）：与轮末 drain 共用 _persist_fills_lock，
          paper 成交缓冲由本方法直接消费落库，drain 不再重复落库
        - 返回 {"contract", "status", "fill_price", "text"} 供 API 响应
          （fill_price 为 Decimal，序列化由 server 层处理）
        """
        async with self._persist_fills_lock:
            return await execute_manual_close(self._deps, contract, drain_fills=self._drain_fills)

    async def manual_cancel_order(self, contract: str, order_id: str) -> dict:
        # 将监控 API 的手动撤单请求转交给统一撤单执行器。
        return await execute_manual_cancel(self._deps, contract, order_id)

    async def _engage_lock(self) -> None:
        """风控锁：内存置位 + 写回 config.yaml（经注入回调，保持分层）；仅加锁瞬间告警。"""
        if self._risk_locked:
            return
        self._risk_locked = True
        self._settings.risk.kill_switch = True
        if self._persist_kill_switch is not None:
            try:  # 持久化失败不拖垮本轮：内存锁仍生效，重启后丢失仅告警日志
                self._persist_kill_switch(True)
            except Exception:
                logger.exception("kill_switch 写回 config.yaml 失败（内存锁仍生效）")
        msg = (
            f"LLM 连续失败 {self._consecutive_failures} 次，"
            "已开启风控锁（kill_switch），暂停开仓，请人工检查"
        )
        logger.error(msg)
        if self._on_alert is not None:
            await maybe_await(self._on_alert(msg))
