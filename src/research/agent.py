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
  绝不向上抛，确保研报失败不影响交易决策循环；唯独外部取消（asyncio.CancelledError，
  如停机 shutdown）原样抛出保持取消语义——成功报告落库前取消/异常走同一失败收尾；
  成功报告落库后，取消与普通异常同口径：禁止双写失败报告，经 _complete_interrupted
  补齐剩余收尾（剩余因果链断点续传落库 + 成功闭合审计 + ok=True 轮末事件），
  取消随后原样抛出、普通异常按成功结果返回；打断可能掐在「成功报告 COMMIT 已执行、
  保存函数未返回」的窗口（内存 report_id 仍为 None），取消与普通异常分支同口径
  不信内存布尔位，按 round_id 反查数据库定口径（反查失败记日志后回落失败收尾）；
  begin_round 自身也有同类窗口（COMMIT 已执行、await 未返回，局部 round_id 仍为
  ""）：两个异常分支先按预分配编号反查认领审计轮（查无或反查失败保持 "" 维持原
  口径），认领后失败收尾正常 end_round 闭合 + 发轮末事件，不留永不闭合的审计轮；
  审计 JSON 快照写入失败降级为日志（SQLite 主表已是真相），不反转已提交结果；
- 初始化（deps/registry/prompt 加载）在 try 边界内：可预见失败落入 _fail 落失败报告，
  不逃逸为仅日志；残余边界：begin_round 自身失败（如 DB 不可用）仍只记日志；
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
from src.utils import identity_of, maybe_await

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

    async def run(
        self, report_type: str = "manual", hours: int = 24, *, round_id: str | None = None
    ) -> dict:
        """执行一次研报。失败返回 {'ok': False, 'error': ...}，绝不向上抛。

        参数：
            report_type: str，研报盘口类型
            hours: int，向前回溯的小时数
            round_id: str | None，调度器手动点火时预分配的审计轮次编号
                （点火响应与 WS 轮始事件同一身份）；None 时由审计层自动生成

        返回：
            dict：执行一次研报。失败返回 {'ok': False, 'error': ...}，绝不向上抛

        异常：
            asyncio.CancelledError：外部取消研报任务时完成收尾后原样抛出；
            成功报告已落库时按成功语义收尾（禁止双写失败报告），否则按失败语义收尾。
            其余 BaseException（如 ExceptionGroup）按失败语义收尾后返回失败结果，
            不再向上抛——保证任何漏网异常下轮次必闭合
        """
        if self._provider is None:
            logger.warning("LLM 未配置，跳过本次研报")
            return {"ok": False, "error": "LLM 未配置", "error_code": "llm_not_configured"}
        expected_contracts = tuple(self._watchlist)
        if not 1 <= hours <= 48:  # L10：与工具层同口径
            return {"ok": False, "error": f"参数错误：hours 须在 1-48 之间（当前 {hours}）"}
        preallocated = round_id or ""  # 先保存入参：下方局部 round_id 以 begin_round 成功为准
        raw_parts: list[str] = []
        round_id = ""
        report_id: int | None = None  # 成功报告落库后置位：收尾据此禁止双写失败报告
        audit_closed = False  # end_round 成功后置位：打断收尾据此补成功闭合（幂等）
        remaining_links: list[dict] = []  # 待落库因果链：flush 断点续传的进度载体
        try:
            # 初始化（deps/registry/prompt 加载）入 try：可预见失败落入 _fail 落失败报告，
            # 不再逃逸为仅日志；begin_round 自身失败（如 DB 不可用）时 round_id 为空，
            # _fail 跳过审计与事件仅落失败报告——此残余边界只记日志，见 _fail
            deps = ResearchToolDeps(
                provider=self._data_provider,
                market_data=self._market_data,
                watchlist_snapshot=expected_contracts,
                repo=self._repo,
                mode=self._settings.mode,
            )
            registry = ResearchToolRegistry(deps)
            full_prompt, _ = self._prompts.system_prompt(render_tool_docs(registry.specs))
            round_id = await self._audit.begin_round(
                self._settings.mode,
                "research",
                full_prompt,
                round_id=preallocated,
                llm_identity=identity_of(self._provider),
            )
            await self._emit_event({"type": "research_round_start", "data": {"round_id": round_id}})
            briefing = await build_preinjection(deps, hours)
            await self._audit.record_context(round_id, briefing)
            text, ask_messages = await self._chat_with_timeout(
                full_prompt, briefing, registry, round_id, raw_parts
            )
            # JSON 重试会放大该阶段调用数，与工具循环同受超时保险丝约束
            retry = self._parse_json_with_retry(
                full_prompt,
                ask_messages,
                registry,
                round_id,
                deps,
                expected_contracts,
                raw_parts,
                text,
            )
            payload = await wait_with_raw(retry, self._timeout, self._audit, round_id, raw_parts)
            report, asset_count = await persist_payload(
                self._repo,
                report_type=report_type,
                payload=payload,
                round_id=round_id,
                deps=deps,
                # 落库所用提示词正文 md5：与版本表同口径，供复盘按版本归因（issue #113）
                research_prompt_md5=self._prompts.body_md5(),
            )
            report_id = report.id
            # 收尾（因果链回填 + end_round）可被打断：report_id 已置位，remaining_links
            # 记录 flush 进度，取消/普通异常分支按成功语义经 _complete_interrupted 补全
            remaining_links = list(deps.pending_causal_links)
            links_saved = await self._flush_causal_links(remaining_links, report.id)
            await self._audit.end_round(round_id, "\n".join(raw_parts))
            audit_closed = True
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
            # 外部取消（保持 asyncio 取消语义，M6）：成功报告未落库走失败收尾；
            # 已落库则禁止再写失败报告（防同轮成功/失败双写），改按成功语义补全收尾。
            # 公共前置覆盖两个提交窗口（begin_round / 成功报告，COMMIT 已执行而调用方
            # 未收到返回）：内存布尔位均不可信，认领审计轮并反查已提交成功研报再定口径
            round_id, committed_id = await self._recover_round_and_committed_id(
                preallocated, round_id, report_id
            )
            if committed_id is not None:
                # 成功报告其实已提交：取消发生在 persist 期间时 flush 快照从未执行，
                # 重新取 pending 链快照断点续传，然后按成功语义补全收尾
                await self._complete_interrupted(
                    list(deps.pending_causal_links),
                    committed_id,
                    round_id,
                    raw_parts,
                    audit_closed,
                )
                raise
            if report_id is None:
                await self._fail(
                    round_id,
                    raw_parts,
                    report_type,
                    asyncio.CancelledError("研报被取消"),
                    preallocated=preallocated,
                )
            else:
                await self._complete_interrupted(
                    remaining_links, report_id, round_id, raw_parts, audit_closed
                )
            raise
        except BaseException as e:
            # 漏网 BaseException（如 ExceptionGroup）：与 Exception 同口径收尾，
            # 保证轮次必闭合、绝不向上抛（CancelledError 分支在前优先生效）
            round_id, committed_id = await self._recover_round_and_committed_id(
                preallocated, round_id, report_id
            )
            if committed_id is not None:
                # 成功报告 COMMIT 后、保存函数返回前抛普通异常：与取消同口径按成功
                # 语义补全并返回成功结果，禁止成功/失败双写
                return await self._recover_committed_success(
                    deps, committed_id, round_id, raw_parts, audit_closed
                )
            if report_id is None:
                return await self._fail(
                    round_id, raw_parts, report_type, e, preallocated=preallocated
                )
            # 成功报告落库后的普通异常：与取消同口径按成功语义补全，返回成功结果
            logger.exception("研报成功落库后收尾异常，按成功语义补全（report_id=%s）", report_id)
            await self._complete_interrupted(
                remaining_links, report_id, round_id, raw_parts, audit_closed
            )
            return success_result(report, round_id, asset_count)

    async def _recover_round_and_committed_id(
        self, preallocated: str, round_id: str, report_id: int | None
    ) -> tuple[str, int | None]:
        """异常/取消收尾的公共前置：认领预分配审计轮 + 反查已提交成功研报编号。

        两个提交窗口的内存布尔位均不可信：begin_round「COMMIT 已执行、await 未返回」
        时局部 round_id 仍为 ""（按预分配编号反查认领）；成功报告「COMMIT 已执行、
        保存函数未返回」时 report_id 仍为 None（按 round_id 反查已提交成功研报）。

        参数：
            preallocated: str，调度器手动点火时预分配的审计轮次编号；空串表示无预分配
            round_id: str，调用方持有的审计轮次编号；空串表示 begin_round 未正常返回
            report_id: int | None，调用方持有的成功研报编号；None 表示保存函数未正常返回

        返回：
            tuple[str, int | None]：(认领后的审计轮次编号, 反查确认的已提交成功研报编号)；
            两个窗口均未命中时分别等于入参 round_id 与 None
        """
        if not round_id:
            round_id = await self._recover_preallocated_round_id(preallocated)
        committed_id: int | None = None
        if report_id is None and round_id:
            committed_id = await self._committed_report_id(round_id)
        return round_id, committed_id

    async def _recover_preallocated_round_id(self, preallocated: str) -> str:
        """认领预分配审计轮：begin_round「COMMIT 已执行、await 未返回」窗口被打断时反查确认轮已创建。

        取消/普通异常可能掐在 begin_round 内部 COMMIT 完成后、await 返回前（aiosqlite
        提交在线程里可能已完成），调用方局部 round_id 仍为 ""——此时按预分配编号反查
        审计轮：查到即认领（后续失败收尾据非空 round_id 正常 end_round 闭合 + 发轮末
        事件）；查无或反查自身失败只记日志返回 ""（维持 begin_round 前失败的既有口径，
        由调度器关机补记负责终态）。

        参数：
            preallocated: str，调度器手动点火时预分配的审计轮次编号；空串表示无预分配

        返回：
            str：审计轮确已创建时返回 preallocated；无预分配、查无此轮或反查失败返回 ""
        """
        if not preallocated:
            return ""
        try:
            round_row = await self._repo.get_audit_round(preallocated)
        except Exception:
            logger.exception("研报预分配审计轮认领反查失败（按未创建处理）")
            return ""
        return preallocated if round_row is not None else ""

    async def _committed_report_id(self, round_id: str) -> int | None:
        """收尾反查：按 round_id 确认该轮成功研报是否其实已提交（error=''），返回其编号。

        取消/普通异常可能掐在「成功 INSERT/COMMIT 已执行、保存函数未返回」的窗口，
        调用方内存中的 report_id 仍为 None——此时不信内存布尔位，反查数据库定口径；
        反查自身失败只记日志并按「未提交」处理（回落失败收尾，不掩盖待传播的取消）。

        参数：
            round_id: str，本轮审计轮次编号

        返回：
            int | None：已提交成功研报的编号；查无成功报告或反查失败返回 None
        """
        try:
            existing = await self._repo.research.find_report_by_round_id(round_id)
        except Exception:
            logger.exception("研报收尾反查成功报告失败（按失败语义收尾）")
            return None
        if existing is not None and existing.error == "":
            return existing.id
        return None

    async def _recover_committed_success(
        self,
        deps: ResearchToolDeps,
        report_id: int,
        round_id: str,
        raw_parts: list[str],
        audit_closed: bool,
    ) -> dict:
        """成功报告 COMMIT 后、保存函数返回前抛普通异常的恢复：按成功语义补全收尾并组装成功结果。

        补全与取消窗口同口径（重取 pending 链快照断点续传 + 成功闭合审计 + ok=True
        事件）；结果中的 report 用反查得到的报告对象替代（该路径没有 persist_payload
        的返回对象），asset_count 从报告 raw_json 的 asset_views 推导
        （save_report_bundle 原子落库，与逐标的结论一一对应）；结果组装失败只记日志，
        退回最小成功结果（报告确已落库，不得因组装失败改写失败）。

        参数：
            deps: ResearchToolDeps，当前模块所需的运行依赖集合（取 pending 因果链快照）
            report_id: int，反查确认的已提交成功研报编号
            round_id: str，本轮审计轮次编号
            raw_parts: list[str]，本轮已累计的 LLM 原始输出
            audit_closed: bool，审计轮是否已正常闭合；未闭合时以成功语义补 end_round

        返回：
            dict：与正常成功同形状的结果（组装失败时为含 ok/report_id/round_id 的最小结果）
        """
        logger.exception("研报成功落库后收尾异常，按成功语义补全（report_id=%s）", report_id)
        await self._complete_interrupted(
            list(deps.pending_causal_links), report_id, round_id, raw_parts, audit_closed
        )
        try:
            report = await self._repo.research.find_report_by_round_id(round_id)
            if report is not None:
                asset_count = len(json.loads(report.raw_json).get("asset_views") or [])
                return success_result(report, round_id, asset_count)
        except Exception:
            logger.exception(
                "已提交研报的成功结果组装失败（report_id=%s，返回最小成功结果）", report_id
            )
        return {"ok": True, "report_id": report_id, "round_id": round_id}

    async def _complete_interrupted(
        self,
        remaining_links: list[dict],
        report_id: int,
        round_id: str,
        raw_parts: list[str],
        audit_closed: bool,
    ) -> None:
        """成功研报落库后被打断（取消或普通异常）的补全收尾：补落剩余因果链 + 成功闭合审计 + ok=True 事件。

        参数：
            remaining_links: list[dict]，尚未落库的因果链（断点续传，就地清空）
            report_id: int，已落库成功研报的编号
            round_id: str，本轮审计轮次编号
            raw_parts: list[str]，本轮已累计的 LLM 原始输出
            audit_closed: bool，审计轮是否已正常闭合；未闭合时以成功语义补 end_round

        返回：
            None：就地补落剩余因果链并按需补闭合审计轮（补闭合失败只记日志，
            不掩盖待传播的取消），并补发轮末 ok=True 事件（重复发送无害，
            前端 exitActive 幂等消化）
        """
        await self._flush_causal_links(remaining_links, report_id)
        if not audit_closed:
            try:
                await self._audit.end_round(round_id, "\n".join(raw_parts))
            except Exception:
                logger.exception("研报收尾补闭合审计轮失败（成功报告已落库，不反转）")
        await self._emit_event(
            {"type": "research_round", "data": {"round_id": round_id, "ok": True}}
        )

    async def _flush_causal_links(self, remaining_links: list[dict], report_id: int) -> int:
        """把剩余因果链回填 report_id 逐条落库（H1：LLM 无需预知 id），支持断点续传。

        每处理一条（成功或单条失败记日志跳过）即从队首移除；取消掐在 save 的 await
        上时队首条目保留，由 _complete_interrupted 重放——极端情况下该条在取消前
        已落库，重放会产生一条重复 INSERT，可接受（优于丢失）。

        参数：
            remaining_links: list[dict]，待落库因果链队列，就地逐条弹出
            report_id: int，研报记录编号

        返回：
            int：本次调用成功落库的因果链条数
        """
        saved = 0
        while remaining_links:
            link = remaining_links[0]
            try:
                await self._repo.research.save_causal_link(report_id=report_id, **link)
                saved += 1
            except Exception:
                logger.exception("因果链落库失败（report_id=%s，跳过该条）", report_id)
            remaining_links.pop(0)
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
        self,
        round_id: str,
        raw_parts: list[str],
        report_type: str,
        exc: BaseException,
        *,
        preallocated: str = "",
    ) -> dict:
        """失败收尾：落 error 报告 + 审计轮 error，绝不向上抛（不变量⑤）。

        round_id 为空（begin_round 前失败）时跳过审计结束与轮末事件；此时失败报告
        仍带预分配轮次编号（preallocated，若有），供调度器关机补记时反查判重。
        落库/审计自身失败只记日志，不把失败升级为异常。

        参数：
            round_id: str，关联的审计轮次编号；空串表示审计轮尚未开启
            raw_parts: list[str]，累计保存 LLM 原始输出的列表
            report_type: str，研报盘口类型
            exc: BaseException，捕获到的原始异常（含 ExceptionGroup 等漏网 BaseException）
            preallocated: str，调度器手动点火时预分配的轮次编号；仅用于 begin_round
                前失败的报告落库（审计与事件仍以真实 round_id 为准）

        返回：
            dict：失败收尾：落 error 报告 + 审计轮 error，绝不向上抛（不变量⑤）
        """
        error = f"{type(exc).__name__}: {exc}"
        logger.exception("研报失败：%s", error)
        try:
            await self._repo.research.save_failed_report(
                report_type=report_type, error=error, round_id=round_id or preallocated
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
