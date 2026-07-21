"""用户手动平仓（监控界面「一键平仓」）与成交落库来源标注。

本模块两件事：
1. 成交落库标注（trade_source_of / persist_fills）：DecisionLoop 轮末 drain 与
   manual_close 共用的 trades 落库路径，source 枚举见 src/memory/models.Trade
2. 手动平仓（close_position / execute_manual_close）：DecisionLoop.manual_close 的
   实现体（单独成文控制 loop.py 行数），与 LLM 平仓（place_order close 分支）
   同一风控判定（_risk_check，is_close=True）与同一落库路径（_record_order）
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from src.agent.tool_handlers import ToolDeps, ToolOutcome
from src.agent.tool_trading import _record_order, _resolve_leverage, _risk_check
from src.audit.logger import get_logger
from src.gateway.base import OrderRequest
from src.memory.repo import Repo
from src.paper.account import FillRecord

logger = get_logger(__name__)


class ManualCloseRiskDenied(Exception):
    """manual_close 被风控拒绝：消息即风控理由，由 server 层映射 HTTP 422。"""


# ---------- 成交落库来源标注（drain / manual_close 共用） ----------


def trade_source_of(fill: FillRecord) -> str:
    """drain 落库时的 source 推导：强平 > LLM 平仓 > LLM 开仓（user_close 由调用方覆盖）。"""
    if fill.order_id == "liquidation":
        return "liquidation"
    if fill.order_id.startswith("tpsl-"):
        return "tpsl_close"
    return "llm_close" if fill.is_close else "llm_open"


async def persist_fills(
    repo: Repo,
    mode: str,
    round_id: str,
    fills: list[FillRecord],
    source_override: str = "",
) -> int:
    """把成交记录逐笔落 trades 表；source_override 非空时覆盖标注（manual_close 用）。

    单笔落库失败不中断：剩余成交继续落，失败落日志（成交已在网关账本，
    缺失可事后对账，重试反而可能双计）。返回落库失败笔数——轮末 drain 忽略；
    manual_close 据以在响应 text 回填警告（同步用户请求不应静默丢记录）。
    """
    failures = 0
    for fill in fills:
        try:
            await repo.save_trade(
                round_id=round_id,
                mode=mode,
                contract=fill.contract,
                size=fill.size,
                price=fill.price,
                fee=fill.fee,
                pnl=fill.realized_pnl,
                source=source_override or trade_source_of(fill),
            )
        except Exception:
            failures += 1
            logger.exception("成交落库失败 round=%s order=%s", round_id[:8], fill.order_id)
    return failures


# ---------- 程序化平仓执行（manual_close 用） ----------


@dataclass
class CloseResult:
    """程序化平仓的执行结果（不经过 LLM 文本层；outcome 携带风控判定与文案）。"""

    outcome: ToolOutcome
    order_id: str = ""
    status: str = ""
    fill_price: Decimal = Decimal(0)


async def close_position(deps: ToolDeps, contract: str, *, trade_source: str = "") -> CloseResult:
    """按 close=True 市价全平：与 place_order 的 close 分支同一风控判定与落库路径。

    风控语义与 LLM 平仓一致（is_close=True：白名单/仓位/杠杆/日亏/日单数/kill_switch
    均豁免，仅价格偏离约束——市价平仓 price=None 亦豁免）。风控拒绝不抛异常，
    返回 outcome.risk_verdict="deny"，由调用方决定（LLM 工具层转文本；
    manual_close 转 ManualCloseRiskDenied）。trade_source 非空时覆盖真实网关
    inline 落库的 trades.source（默认 close → llm_close）。
    """
    positions = deps.gateway.list_positions()
    had_position = any(p.contract == contract for p in positions)  # 下单前快照（防文本谎称）
    leverage, _ = _resolve_leverage(contract, None, positions)  # 平仓不调整杠杆
    deny = await _risk_check(
        deps, contract, size=Decimal(0), price=None, is_close=True, leverage=leverage
    )
    if deny is not None:
        return CloseResult(outcome=deny)
    req = OrderRequest(contract=contract, size=Decimal(0), close=True)
    result = deps.gateway.place_order(req)
    warning = await _record_order(deps, result, req, positions, trade_source=trade_source)
    if not had_position or result.finish_as == "no_position":
        # 无真实成交（paper 报 no_position；mock/真实网关无持仓 close 为 no-op），不谎称成交均价
        text = f"手动平仓：{contract} 当前无持仓"
    else:
        text = (
            f"手动平仓：{contract}，订单号 {result.id}，状态 {result.status}，"
            f"成交均价 {result.fill_price}"
        )
    if warning:
        text += f"；警告：{warning}"
    return CloseResult(ToolOutcome(text, "allow"), result.id, result.status, result.fill_price)


async def execute_manual_close(
    deps: ToolDeps,
    contract: str,
    *,
    drain_fills: Callable[[], list[FillRecord]] | None,
) -> dict:
    """manual_close 实现体：平仓 → 成交落库（source=user_close）→ 返回 API 响应字典。

    风控拒绝抛 ManualCloseRiskDenied；网关错误（如合约不存在）以 GatewayError 上抛。
    调用方（DecisionLoop.manual_close）须持有落库锁（与轮末 drain 同一把）。

    paper 双计处理（直接消费）：成交后立即取走网关缓冲中的全部 fill——本单成交
    按 user_close 落库；缓冲里夹带的其他 fill（同轮 LLM 已下未落库的成交、轮间
    强平等）按标准标注一并落库。缓冲已清空，轮末 drain 无货可落，天然无双计。
    真实网关无缓冲：trades 由 close_position 经 _record_order 内联落库
    （trade_source 覆盖为 user_close）。
    """
    cr = await close_position(deps, contract, trade_source="user_close")
    if cr.outcome.risk_verdict == "deny":
        logger.warning("手动平仓被风控拒绝 contract=%s：%s", contract, cr.outcome.risk_reason)
        raise ManualCloseRiskDenied(cr.outcome.risk_reason or cr.outcome.text)
    text = cr.outcome.text
    if drain_fills is not None:
        fills = drain_fills()
        mine = [f for f in fills if f.order_id == cr.order_id]
        others = [f for f in fills if f.order_id != cr.order_id]
        # 夹带 fill 失败走 drain 语义（仅日志）；本单失败必须让用户知情
        await persist_fills(deps.repo, deps.mode, deps.round_id, others)
        failed = await persist_fills(deps.repo, deps.mode, deps.round_id, mine, "user_close")
        if failed:
            text += f"；警告：{failed} 笔平仓成交仅本地记录失败（成交已生效，勿重复平仓）"
    return {
        "contract": contract,
        "status": cr.status,
        "fill_price": cr.fill_price,
        "text": text,
    }
