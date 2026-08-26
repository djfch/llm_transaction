"""决策循环：组装上下文 → 调 LLM → 逐个执行工具调用 → 审计归档。

不变量：
- 每轮经 AuditTrail 生成 round_id；审计行（含 prompt 快照）先于上下文构建落库，
  保证任何失败都在 audit_rounds 留痕迹；上下文构建成功后回填快照
- 每次工具调用都落审计（入参/风控判定/结果/耗时），轮结束写 JSON 全文快照
- LLM 调用或输出解析失败 → 本轮不再执行任何工具调用（不交易），失败落审计
- 每轮结束（含失败轮）经 drain_fills 把 paper 网关成交落 trades 表；
  真实网关无此钩子，trades 由 ExchangeFillSync 按交易所真实成交回报落库
  （见 agent/fill_sync.py）
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
from src.agent.fill_persist import FillPersister
from src.agent.manual_cancel import execute_manual_cancel
from src.agent.manual_close import execute_manual_close
from src.agent.prompts import PromptLoader
from src.agent.providers.base import LLMProvider, ToolCall
from src.agent.tool_handlers import DailyStatsFn, ToolDeps, ToolOutcome
from src.agent.tools import ToolRegistry
from src.audit.logger import get_logger
from src.audit.trail import AuditTrail
from src.config import Settings
from src.gateway.async_io import set_orphan_write_handler
from src.gateway.base import Gateway
from src.market.candles import CandleCache
from src.market.indicator_service import IndicatorService
from src.market.triggers import TriggerManager
from src.memory.repo import Repo
from src.risk.engine import RiskEngine
from src.risk.models import DailyStats
from src.utils import identity_of, maybe_await

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
    """当日统计：已实现盈亏与开仓单数均按 mode 过滤（自然日，本地时区）。

    参数：
        repo: Repo，交易与审计数据仓库
        mode: str，运行模式
    返回：
        DailyStats，当日统计：已实现盈亏与开仓单数均按 mode 过滤（自然日，本地时区）
    """
    now = time.localtime()
    day_start = time.mktime((now.tm_year, now.tm_mon, now.tm_mday, 0, 0, 0, 0, 0, -1))
    return await repo.daily_stats(mode, day_start)


class DecisionLoop:
    """LLM 自主决策循环。所有依赖构造期注入，provider 可空、可热替换。

    provider 为 None（LLM 未配置，如缺 API key）时 run_once 跳过本轮：
    不落审计、不计连续失败；配置补齐后经 set_provider 热替换即恢复决策。

    可选依赖：
    - drain_fills：paper 网关成交缓冲泄放钩子（PaperGateway.drain_fills）；
      真实网关为 None（trades 由 fill_sync 按交易所成交回报落库，不经工具层）
    - fill_persister：统一成交写入入口（轮末兜底 drain / manual_close / 行情即时
      drain 三方共用，见 fill_persist.py）；缺省时按 repo/mode/notify_event 自建
    - persist_kill_switch：风控锁写回 config.yaml 的回调（保持 agent 层不碰 config_io）
    - audit：共享 AuditTrail；缺省时按 settings.audit 自建（快照目录一致）
    - indicator_service：技术指标服务（get_indicators 工具与上下文短名单行共用）；
      None 时工具如实回报未接入、上下文省略指标行
    - indicator_shortlist：上下文指标行短名单来源回调（每次构建重读，复盘修订
      短名单后下一轮即生效）；None 时 ContextBuilder 回退内置基线
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
        fill_persister: FillPersister | None = None,
        audit: AuditTrail | None = None,
        indicator_service: IndicatorService | None = None,
        indicator_shortlist: Callable[[], list[str]] | None = None,
        max_turns: int = 8,
    ) -> None:
        """注入全部依赖，装配工具注册表与上下文构建器。

        参数：
            settings: Settings，全局配置（运行模式/风控/LLM/审计/研报等）
            watchlist: list[str]，监控合约列表
            provider: LLMProvider | None，LLM 提供者；None 表示未配置，run_once 跳过本轮
            gateway: Gateway，交易所网关（真实或 paper 模拟）
            risk_engine: RiskEngine，风控引擎
            repo: Repo，SQLite 持久化仓库
            candles: CandleCache，K线缓存
            triggers: TriggerManager，价格触发器
            prompt_loader: PromptLoader，系统提示词（策略书）加载器
            set_next_wake: Callable[[int], int] | None，回写下次唤醒时间的回调；None 时不可调度
            on_alert: AlertCallback | None，告警回调；None 时仅记日志不发外部告警
            daily_stats_fn: DailyStatsFn | None，当日统计来源；None 时按 repo/mode 走默认实现
            drain_fills: Callable[[], list] | None，paper 网关成交缓冲泄放钩子；真实网关为 None
            persist_kill_switch: Callable[[bool], None] | None，风控锁写回 config.yaml 的回调；
                None 时仅内存置位，重启后丢失
            notify_event: Callable[[dict], None] | None，事件通知回调（成交落库等场景透传）
            fill_persister: FillPersister | None，统一成交写入入口；None 时按 repo/mode 自建
            audit: AuditTrail | None，共享审计器；None 时按 settings.audit 自建
            indicator_service: IndicatorService | None，技术指标服务；None 时工具如实回报未接入
            indicator_shortlist: Callable[[], list[str]] | None，上下文指标短名单来源回调；
                None 时 ContextBuilder 回退内置基线
            max_turns: int，单轮决策内 LLM 工具往返的最大轮次（下限 1），默认 8

        返回：
            None，就地完成依赖注入与 ToolDeps/ToolRegistry/ContextBuilder 装配
        """
        self._settings = settings
        self._provider = provider
        self._repo = repo
        self._prompts = prompt_loader
        self._on_alert = on_alert
        self._max_turns = max(1, max_turns)
        self._consecutive_failures = 0
        self._risk_locked = False
        self._drain_fills = drain_fills
        self._persister = fill_persister or FillPersister(repo, settings.mode, notify_event)
        self._persist_kill_switch = persist_kill_switch
        self._notify_event = notify_event
        self._audit = audit or AuditTrail(repo, settings.audit)
        self._deps = ToolDeps(
            gateway=gateway,
            risk_engine=risk_engine,
            risk_config=settings.risk,
            watchlist=watchlist,
            repo=repo,
            candles=candles,
            triggers=triggers,
            indicator_service=indicator_service,
            daily_stats_fn=daily_stats_fn or (lambda: default_daily_stats(repo, settings.mode)),
            research_config=settings.research,
            mode=settings.mode,
            set_next_wake=set_next_wake,
            notify_event=notify_event,
        )
        # 工具层（如杠杆回滚失败）经此回调触发同一套风控锁（内存+持久化+告警）
        self._deps.engage_kill_switch = self._engage_lock
        # 已 dispatch 到交易所的写请求被取消（结果无人接收）时的兜底：审计 + 风控锁
        set_orphan_write_handler(self._on_orphan_write)
        self._registry = ToolRegistry(self._deps)
        self._context = ContextBuilder(
            gateway,
            repo,
            candles,
            triggers,
            watchlist,
            indicator_service=indicator_service,
            indicator_shortlist=indicator_shortlist,
            research_config=settings.research,
        )

    @property
    def consecutive_failures(self) -> int:
        """读取 LLM 连续失败计数（供调度器/监控层透出）。

        参数：无

        返回：
            int：连续失败次数，决策成功一轮后清零
        """
        return self._consecutive_failures

    @property
    def risk_locked(self) -> bool:
        """读取风控锁是否已置位（连续失败达上限后锁定开仓）。

        参数：无

        返回：
            bool：True 表示风控锁（kill_switch）已生效
        """
        return self._risk_locked

    @property
    def llm_configured(self) -> bool:
        """provider 是否已配置（status_provider 透出 /api/status 用）。

        参数：无
        返回：
            bool，provider 是否已配置（status_provider 透出 /api/status 用）
        """
        return self._provider is not None

    def set_provider(self, provider: LLMProvider) -> None:
        """热替换 LLM provider（配置前端化：改 key/模型重建后下轮决策即生效）。

        参数：
            provider: LLMProvider，新的 LLM 提供方；None 表示未配置
        返回：
            None，热替换 LLM provider（配置前端化：改 key/模型重建后下轮决策即生效）
        """
        self._provider = provider

    async def run_once(self, wake_source: str) -> RoundResult:
        """执行一轮决策。失败不向上抛；每轮结束（含失败轮）统一 drain 成交落库。

        参数：
            wake_source: str，本轮唤醒来源
        返回：
            RoundResult，执行一轮决策。失败不向上抛；每轮结束（含失败轮）统一 drain 成交落库
        """
        if self._provider is None:
            # LLM 未配置（缺 key）：跳过本轮，不落审计、不计连续失败；
            # 但先泄放成交缓冲——paper 强平/挂单成交与 LLM 无关，跳轮不得滞留丢失
            await self._drain_round_fills()
            logger.warning("LLM 未配置，跳过本轮决策")
            return RoundResult(round_id="", ok=False, wake_source=wake_source, error="LLM 未配置")
        prompt, prompt_md5 = self._prompts.system_prompt(self._registry.specs)
        strategy_md5 = self._prompts.body_md5()  # 策略书原文 md5，落库供复盘按版本关联
        # 先落审计行（空上下文快照）：后续任何失败都在 audit_rounds 留痕迹
        round_id = await self._audit.begin_round(
            self._settings.mode,
            wake_source,
            prompt,
            strategy_md5=strategy_md5,
            llm_identity=identity_of(self._provider),
        )
        await self._emit_round_start(round_id, wake_source)
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
        await self._drain_round_fills()
        return result

    async def _emit_round_start(self, round_id: str, wake_source: str) -> None:
        """在审计行落库后广播轮开始事件；广播失败不拖垮决策。

        参数：
            round_id: str，已落库的决策轮编号
            wake_source: str，本轮唤醒来源

        返回：
            None，存在回调时发送 round_start 事件
        """
        if self._notify_event is None:
            return
        try:
            payload = {
                "type": "round_start",
                "data": {"round_id": round_id, "wake_source": wake_source},
            }
            await maybe_await(self._notify_event(payload))
        except Exception:
            logger.exception("round=%s 开始事件广播失败（继续执行本轮）", round_id[:8])

    async def _chat_loop(
        self, prompt: str, ctx: AgentContext, round_id: str
    ) -> tuple[str, str, int]:
        """多轮对话：LLM 返回工具调用就执行并回填结果，直到无调用或达轮次上限。

        参数：
            prompt: str，完整系统提示词
            ctx: AgentContext，本轮上下文快照
            round_id: str，决策轮标识
        返回：
            tuple[str, str, int]，多轮对话：LLM 返回工具调用就执行并回填结果，直到无调用或达轮次上限
        """
        messages: list[dict] = [{"role": "user", "content": ctx.text}]
        schemas = self._registry.schemas()
        raw_parts: list[str] = []
        text, total = "", 0
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
                return text, "\n".join(raw_parts), total
            for call in resp.tool_calls:
                outcome = await self._execute_call(round_id, total + 1, call)
                total += 1
                messages.append(self._provider.tool_result_message(call, outcome.text))
        logger.warning("round=%s 达到最大工具轮次 %d，强制结束", round_id[:8], self._max_turns)
        return text, "\n".join(raw_parts), total

    async def _execute_call(self, round_id: str, seq: int, call: ToolCall) -> ToolOutcome:
        """执行一次工具调用并落审计（入参/风控判定/结果/耗时）。

        参数：
            round_id: str，决策轮标识
            seq: int，工具调用序号
            call: ToolCall，模型返回的工具调用
        返回：
            ToolOutcome，执行一次工具调用并落审计（入参/风控判定/结果/耗时）
        """
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

        参数：
            round_id: str，决策轮标识
            prompt_md5: str，完整提示词摘要
            strategy_md5: str，策略正文摘要
            wake_source: str，本轮唤醒来源
            ctx: AgentContext | None，本轮上下文快照
            exc: Exception，本轮失败异常
        返回：
            RoundResult，失败收尾：计数、落决策与审计（error 字段），达标则加锁告警
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
        await self._audit.end_round(round_id, None, error=error)
        if self._consecutive_failures >= self._settings.llm.max_consecutive_failures:
            await self._engage_lock()
        return RoundResult(round_id=round_id, ok=False, wake_source=wake_source, error=error)

    async def _drain_round_fills(self) -> None:
        """paper 网关成交缓冲落 trades 表（真实网关无此钩子，由工具层直接落库）。

        经 FillPersister 与 manual_close、行情即时 drain 互斥：缓冲只被 drain 走
        一次，轮末兜底不会重复落库（归属继承与标注规则见 fill_persist.py）。

        参数：无
        返回：
            None，paper 网关成交缓冲落 trades 表（真实网关无此钩子，由工具层直接落库）
        """
        if self._drain_fills is None:
            return
        await self._persister.drain_persist(self._drain_fills)

    async def manual_close(self, contract: str) -> dict:
        """用户手动平仓（监控界面）：与 LLM 平仓同一风控路径，成交标注 source=user_close。

        - 风控拒绝抛 ManualCloseRiskDenied（消息为风控理由，server 层映射 HTTP 422）；
          网关错误（如合约不存在）以 GatewayError 原样上抛
        - 决策轮进行中也可调用（用户操作优先）：FillPersister 锁由 execute_manual_close
          内部持有，覆盖「下单→drain→落库」全程，行情即时 drain 抢不走其成交
        - 返回 {"contract", "status", "fill_price", "text"} 供 API 响应
          （fill_price 为 Decimal，序列化由 server 层处理）

        参数：
            contract: str，待手动平仓的合约标识
        返回：
            dict，用户手动平仓（监控界面）：与 LLM 平仓同一风控路径，成交标注 source=user_close
        """
        return await execute_manual_close(
            self._deps, contract, drain_fills=self._drain_fills, persister=self._persister
        )

    async def manual_cancel_order(self, contract: str, order_id: str) -> dict:
        """用户手动撤单（监控 API）：转交统一撤单执行器完成撤单与本地记录同步。

        参数：
            contract: str，合约名（如 BTC_USDT）
            order_id: str，待撤销的订单 ID

        返回：
            dict：撤单结果（id/contract/status/finish_as/warning，供 API 响应；
            warning 非空表示网关已撤单但本地记录同步失败，提示勿重试撤单）
        """
        return await execute_manual_cancel(self._deps, contract, order_id)

    def notify_paper_reset(self) -> None:
        """模拟账户重置后上调重置代际：使在途增仓写的代际比对失效而中止（issue #81）。

        重置清空账户后，旧 Agent 轮已通过风控、尚未落单的增仓写若照常提交，会在
        新账户上重新开仓；本方法由 bootstrap 的 paper_reset 适配在重置后调用，
        与下单/改单路径捕获的 reset0 锚点构成原子对（paper 内联同线程，无交错窗口）。

        参数：无

        返回：
            None，就地上调 ToolDeps.reset_epoch 计数
        """
        self._deps.reset_epoch[0] += 1

    def _on_orphan_write(self, op_name: str) -> None:
        """已 dispatch 到交易所的写请求因调用方取消/超时导致结果无人接收时的兜底。

        审计记 critical 日志，并异步触发风控锁（fail-closed：交易所可能已执行，
        本地状态未知，禁止继续自动交易，待人工对账）。由 async_io 取消分支同步
        调用（不能 await，独立任务调度加锁）。

        参数：
            op_name: str，被取消的写操作名（如 place_order）

        返回：
            None，记审计日志并调度风控锁任务
        """
        logger.critical(
            "写请求 %s 已下发交易所但调用方被取消/超时，结果无人接收："
            "按状态未知处理并触发风控锁，请人工对账",
            op_name,
        )
        try:
            asyncio.get_running_loop().create_task(
                self._engage_lock(f"写请求 {op_name} 已下发但结果被取消，状态未知，请人工对账")
            )
        except RuntimeError:  # 事件循环已关闭：日志留痕即可，重启后人工介入
            pass

    async def _engage_lock(self, reason: str | None = None) -> None:
        """风控锁：内存置位 + 写回 config.yaml（经注入回调，保持分层）；仅加锁瞬间告警。

        参数：
            reason: str | None，自定义触发原因（如杠杆回滚失败）；None 时用连续失败默认文案

        返回：
            None，风控锁：内存置位 + 写回 config.yaml（经注入回调，保持分层）；仅加锁瞬间告警
        """
        if self._risk_locked:
            return
        self._risk_locked = True
        self._settings.risk.kill_switch = True
        if self._persist_kill_switch is not None:
            try:  # 持久化失败不拖垮本轮：内存锁仍生效，重启后丢失仅告警日志
                self._persist_kill_switch(True)
            except Exception:
                logger.exception("kill_switch 写回 config.yaml 失败（内存锁仍生效）")
        msg = reason or (
            f"LLM 连续失败 {self._consecutive_failures} 次，"
            "已开启风控锁（kill_switch），暂停开仓，请人工检查"
        )
        logger.error(msg)
        if self._on_alert is not None:
            await maybe_await(self._on_alert(msg))
