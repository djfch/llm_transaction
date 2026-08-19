"""用户手动平仓（监控界面「一键平仓」）。

本模块两件事：
1. 程序化平仓（close_position）：与 LLM 平仓（place_order close 分支）同一风控
   判定（_risk_check，is_close=True）与同一落库路径（_record_order）
2. 手动平仓（execute_manual_close）：DecisionLoop.manual_close 的实现体（单独
   成文控制 loop.py 行数）；持 FillPersister 锁覆盖「平仓→drain→落库」全程，
   本单成交标 user_close；成交归属继承/标注规则/失效信号见 fill_persist.py
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from src.agent.fill_persist import FillPersister
from src.agent.tool_handlers import ToolDeps, ToolOutcome
from src.agent.tool_trading import _record_order, _resolve_leverage, _risk_check
from src.audit.logger import get_logger
from src.gateway.async_io import PRIORITY_HIGH, run_gateway_io
from src.gateway.base import OrderRequest
from src.paper.account import FillRecord

logger = get_logger(__name__)


class ManualCloseRiskDenied(Exception):
    """manual_close 被风控拒绝：消息即风控理由，由 server 层映射 HTTP 422。"""


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
    manual_close 转 ManualCloseRiskDenied）。trade_source 非空时透传给 orders.trade_source
    （manual_close 传 user_close，供交易所真实成交回报分类归属）。

    参数：
        deps: ToolDeps，网关、风控、订单仓库等共享交易依赖
        contract: str，待全部平仓的合约名称
        trade_source: str，写入订单记录的交易来源标识

    返回：
        CloseResult，包含风控判定、订单状态与成交价格的平仓结果
    """
    positions = await run_gateway_io(deps.gateway.list_positions, priority=PRIORITY_HIGH)
    had_position = any(p.contract == contract for p in positions)  # 下单前快照（防文本谎称）
    leverage, _ = _resolve_leverage(contract, None, positions)  # 平仓不调整杠杆
    deny = await _risk_check(
        deps,
        contract,
        size=Decimal(0),
        price=None,
        is_close=True,
        leverage=leverage,
        priority=PRIORITY_HIGH,  # 人工平仓全程高优先级：风控读取不掉回普通队尾
    )
    if deny is not None:
        return CloseResult(outcome=deny)
    req = OrderRequest(contract=contract, size=Decimal(0), close=True)
    result = await run_gateway_io(deps.gateway.place_order, req, priority=PRIORITY_HIGH)
    warning = await _record_order(deps, result, req, trade_source=trade_source)
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
    persister: FillPersister,
) -> dict:
    """manual_close 实现体：平仓 → 成交落库（source=user_close）→ 返回 API 响应字典。

    风控拒绝抛 ManualCloseRiskDenied；网关错误（如合约不存在）以 GatewayError 上抛。
    全程持 FillPersister 锁：close_position 内 place_order 同步产生 fill，此后若
    释放锁，行情即时 drain 可能抢先落库并把本单标成 llm_close；持锁到底保证
    本函数 drain 到该笔并标 user_close。锁不可重入，锁内只能调 persist_locked。

    paper 双计处理（直接消费）：成交后立即取走网关缓冲中的全部 fill——本单成交
    按 user_close 落库；缓冲里夹带的其他 fill（同轮 LLM 已下未落库的成交、轮间
    强平等）按标准标注一并落库。缓冲已清空，轮末 drain 无货可落，天然无双计。
    真实网关无缓冲：trades 由 ExchangeFillSync 按交易所真实成交回报落库，订单行
    已带 trade_source=user_close 供归属判定。

    参数：
        deps: ToolDeps，网关、风控与订单仓库等共享交易依赖
        contract: str，用户要求平仓的合约名称
        drain_fills: Callable[[], list[FillRecord]] | None，提取模拟网关待落库成交的回调
        persister: FillPersister，串行化并持久化成交记录的服务

    返回：
        dict，包含合约、订单状态、成交价格和用户可读说明的 API 响应

    异常：
        ManualCloseRiskDenied: 风控拒绝本次手动平仓时抛出
    """
    async with persister.lock:
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
            await persister.persist_locked(others)
            failed = await persister.persist_locked(mine, source_override="user_close")
            if failed:
                text += f"；警告：{failed} 笔平仓成交仅本地记录失败（成交已生效，勿重复平仓）"
    return {
        "contract": contract,
        "status": cr.status,
        "fill_price": cr.fill_price,
        "text": text,
    }
