"""杠杆设置/回滚/对账辅助（place_order 的杠杆安全边界）。

从 tool_trading 拆出以控制单文件体量。核心不变量：
- 声明杠杆下单前，先快照当前 (杠杆, 保证金模式) 作为回滚锚点；
- 交易所明确拒绝（订单确定未创建）才回滚，回滚后必须重读核验；
- 调杠杆结果未知且异常非明确拒绝（超时/断连）时，必须延迟复核确认状态稳定，
  防止请求在服务端迟到提交后本地误判"未生效"；
- 回滚失败、核验不一致、订单状态未知、杠杆状态未知，一律触发风控锁（fail closed）。
"""

from __future__ import annotations

import asyncio

from src.agent.tool_handlers import ToolDeps, ToolOutcome
from src.audit.logger import get_logger
from src.gateway.base import GatewayError, OrderRequest, OrderResult, OrderStateUnknown, Position
from src.utils import maybe_await

logger = get_logger(__name__)

# 结果未知（超时/断连）时的延迟复核参数：首次读到旧值不能证明写入未生效
# （请求可能已在服务端排队、随后迟到提交），须轮询确认状态稳定后才可宣告未生效
_UNKNOWN_SETTLE_RETRIES = 3
_UNKNOWN_SETTLE_DELAY_S = 1.0


def _prev_leverage_state(positions: list[Position], contract: str) -> tuple[int, str] | None:
    """从持仓快照提取合约当前 (杠杆倍数, 保证金模式)，供失败回滚与对账使用。

    参数：
        positions: list[Position]，当前持仓快照列表
        contract: str，目标合约
    返回：
        tuple[int, str] | None，(杠杆倍数, 保证金模式)；无有效持仓（真实网关会返回
        size=0 的历史条目，与 mock/paper 口径统一按无持仓处理），或全仓持仓但
        cross_leverage_limit 缺失/非整数（无可信有效杠杆，非整数如 lever=4.35 回退值
        无法经 int set_leverage 精确回滚）时返回 None
    """
    pos = next((p for p in positions if p.contract == contract and p.size != 0), None)
    if pos is None:
        return None
    if pos.margin_mode == "cross":
        limit = pos.cross_leverage_limit
        if limit is None or limit <= 0 or int(limit) != limit:
            return None
        return max(int(limit), 1), "cross"
    return max(int(pos.leverage), 1), "isolated"


async def _recheck_prev_state(
    deps: ToolDeps, contract: str, prev_state: tuple[int, str] | None, *, will_modify: bool
) -> ToolOutcome | None:
    """风控 await 窗口后重读杠杆快照：状态被并发修改时触发风控锁并返回拒绝文案。

    参数：
        deps: ToolDeps，当前模块所需的依赖集合
        contract: str，目标合约
        prev_state: tuple[int, str] | None，风控前捕获的 (杠杆, 模式) 快照
        will_modify: bool，本次调用是否将修改杠杆（不修改则无需核验，直接返回 None）

    返回：
        ToolOutcome | None，不修改杠杆或状态一致返回 None；被并发修改返回拒绝文案（已触发风控锁）
    """
    if not will_modify:
        return None
    latest = _prev_leverage_state(deps.gateway.list_positions(), contract)
    if latest == prev_state:
        return None
    await _engage_kill(deps, f"{contract} 风控期间杠杆状态变化（{prev_state} → {latest}）")
    return ToolOutcome(
        f"风控期间杠杆状态被并发修改（{prev_state} → {latest}），"
        "已开启风控锁，订单未提交，请人工核对",
        "deny",
        "杠杆状态并发变化",
    )


async def _engage_kill(deps: ToolDeps, reason: str) -> None:
    """触发风控锁：优先走注入回调（持久化 + 告警），无回调时退化为内存置位 + 日志。

    参数：
        deps: ToolDeps，当前模块所需的依赖集合
        reason: str，触发原因（写入日志与告警文案）
    返回：
        None，风控锁触发后返回（仅触发动作，不产生业务文本）
    """
    logger.error("触发风控锁：%s", reason)
    if deps.engage_kill_switch is not None:
        await maybe_await(deps.engage_kill_switch(reason))
        return
    deps.risk_config.kill_switch = True  # 无回调时至少保证本轮起内存锁生效


async def _rollback_leverage(
    deps: ToolDeps,
    contract: str,
    prev_state: tuple[int, str] | None,
    expected_current: tuple[int, str] | None,
) -> str:
    """尽力回滚杠杆并重读核验；回滚失败、核验不一致或存在并发修改时触发风控锁。

    参数：
        deps: ToolDeps，当前模块所需的依赖集合
        contract: str，目标合约
        prev_state: tuple[int, str] | None，修改前的 (杠杆倍数, 保证金模式)；None 表示无可信状态
        expected_current: tuple[int, str] | None，本调用设置的目标状态（回滚前 CAS 核验用）；
            None 表示本调用未修改杠杆，跳过核验
    返回：
        str，面向操作员的回滚结果描述（成功、无状态可回滚、并发修改中止、失败或核验不一致）
    """
    if prev_state is None:
        logger.warning("下单失败但无可信回滚状态: %s", contract)
        return "杠杆已修改且无可信的修改前状态（无持仓或全仓杠杆未知），请人工核对杠杆设置"
    if expected_current is not None:
        # CAS 核验：仅当当前状态仍是本调用设置的目标值时才回滚，
        # 否则说明存在并发修改，盲写旧快照会覆盖他人改动
        try:
            current = _prev_leverage_state(deps.gateway.list_positions(), contract)
        except Exception:
            current = None
        if current != expected_current:
            await _engage_kill(
                deps, f"{contract} 回滚前核验异常（预期当前 {expected_current}，实际 {current}）"
            )
            return (
                f"回滚中止：当前杠杆状态 {current} 已不是本次设置的 {expected_current}，"
                "存在并发修改，已开启风控锁，请人工核对"
            )
    try:
        deps.gateway.set_leverage(contract, prev_state[0], prev_state[1])
    except Exception as e:  # 回滚自身失败：fail closed
        logger.exception("杠杆回滚失败: %s", contract)
        await _engage_kill(deps, f"{contract} 杠杆回滚失败（{type(e).__name__}: {e}）")
        return f"杠杆回滚失败（{type(e).__name__}: {e}），已开启风控锁，请人工核对"
    try:
        state = _prev_leverage_state(deps.gateway.list_positions(), contract)
    except Exception:  # 重读失败视同核验不一致
        state = None
    if state != prev_state:
        await _engage_kill(
            deps, f"{contract} 杠杆回滚核验不一致（期望 {prev_state}，实际 {state}）"
        )
        return (
            f"杠杆回滚后核验不一致（期望 {prev_state[0]}（{prev_state[1]}），实际 {state}），"
            "已开启风控锁，请人工核对"
        )
    return f"杠杆与保证金模式已回滚至 {prev_state[0]}（{prev_state[1]}）"


async def _place_with_rollback(
    deps: ToolDeps,
    req: OrderRequest,
    prev_state: tuple[int, str] | None,
    *,
    leverage_modified: bool,
    target_state: tuple[int, str] | None = None,
) -> OrderResult | ToolOutcome:
    """杠杆就位后下单；被拒时回滚（仅当改过杠杆），状态未知或不明确异常时触发风控锁。

    参数：
        deps: ToolDeps，当前模块所需的依赖集合
        req: OrderRequest，订单请求
        prev_state: tuple[int, str] | None，修改前的 (杠杆, 模式) 状态，用于失败后回滚
        leverage_modified: bool，本次调用是否实际修改过杠杆（未修改则失败无需回滚）
        target_state: tuple[int, str] | None，本调用设置的目标 (杠杆, 模式)，回滚前 CAS 核验用
    返回：
        OrderResult | ToolOutcome，成功返回订单结果；明确拒绝、状态未知或异常不明返回提示文案
    """
    try:
        return deps.gateway.place_order(req)
    except OrderStateUnknown as e:
        # 订单可能已创建，回滚杠杆会与实际持仓不一致：保持杠杆 + fail closed
        logger.warning("下单状态未知: %s err=%s", req.contract, e)
        await _engage_kill(deps, f"{req.contract} 下单状态未知（{e}）")
        note = "；杠杆保持当前设置" if leverage_modified else ""
        return ToolOutcome(
            f"下单状态未知：{e}{note}，已开启风控锁，请人工核对持仓与订单状态，禁止盲目重试"
        )
    except GatewayError as e:
        # 交易所明确拒绝，订单确定未创建，可安全回滚
        if not leverage_modified:
            return ToolOutcome(f"下单失败：{e}")
        return ToolOutcome(
            f"下单失败：{e}；"
            f"{await _rollback_leverage(deps, req.contract, prev_state, target_state)}"
        )
    except Exception as e:
        # 非网关异常：订单是否创建不明，与 OrderStateUnknown 同策——不回滚、fail closed。
        # 不上抛：ToolRegistry 会把异常转成"工具内部错误"诱导 LLM 重试（重试即重单）
        logger.exception("下单非网关异常，订单状态不明: %s", req.contract)
        await _engage_kill(
            deps, f"{req.contract} 下单出现非网关异常（{type(e).__name__}），订单状态不明"
        )
        return ToolOutcome(
            f"下单结果不明（{type(e).__name__}: {e}），已开启风控锁，"
            "请人工核对持仓与订单状态，禁止盲目重试"
        )


async def _confirm_not_applied_delayed(
    deps: ToolDeps,
    req: OrderRequest,
    *,
    prev_state: tuple[int, str],
    error: Exception,
) -> ToolOutcome:
    """结果未知异常下的延迟复核：轮询确认杠杆稳定在修改前状态后才宣告未生效。

    超时/断连时请求可能已在服务端排队，首次读到旧值不等于写入未生效；复核窗口内
    观察到任何变化（即使恰为目标值，也无法排除并发修改或迟到提交仍在途）一律 fail
    closed 开启风控锁；只有全程稳定在修改前状态才可安全宣告未生效。

    参数：
        deps: ToolDeps，当前模块所需的依赖集合
        req: OrderRequest，订单请求
        prev_state: tuple[int, str]，修改前的 (杠杆, 模式) 状态（已确认首次读取等于该值）
        error: Exception，调杠杆时捕获的原始异常
    返回：
        ToolOutcome，稳定返回未生效提示；状态变化或读取失败返回风控锁文案
    """
    for _ in range(_UNKNOWN_SETTLE_RETRIES):
        await asyncio.sleep(_UNKNOWN_SETTLE_DELAY_S)
        try:
            state = _prev_leverage_state(deps.gateway.list_positions(), req.contract)
        except Exception:
            state = None
        if state != prev_state:  # 迟到提交落地/并发修改/读取失败：无法证明未生效，fail closed
            await _engage_kill(
                deps, f"{req.contract} 调杠杆结果未知，延迟复核发现状态异常（实际 {state}）"
            )
            return ToolOutcome(
                f"调杠杆结果未知：延迟复核发现杠杆状态变化（{prev_state} → {state}），"
                "存在迟到提交或并发修改，已开启风控锁，订单未提交，请人工核对杠杆与持仓状态"
            )
    return ToolOutcome(
        f"调杠杆未生效（{type(error).__name__}: {error}），经 {_UNKNOWN_SETTLE_RETRIES} 次延迟复核"
        "杠杆仍稳定为修改前状态，订单未提交，可人工核对后重试"
    )


async def _reconcile_leverage_unknown(
    deps: ToolDeps,
    req: OrderRequest,
    *,
    apply_leverage: int,
    margin_mode: str,
    prev_state: tuple[int, str] | None,
    error: Exception,
) -> OrderResult | ToolOutcome:
    """调杠杆结果未知时读取持仓对账：已达目标继续下单，其余按异常类别分流。

    交易所明确拒绝（GatewayError，Gate 已回错误响应）且读回旧值时可直接宣告未生效；
    超时/断连等真正结果未知的异常须进入延迟复核，防止迟到提交后静默改杠杆。

    参数：
        deps: ToolDeps，当前模块所需的依赖集合
        req: OrderRequest，订单请求
        apply_leverage: int，本次尝试设置的杠杆倍数
        margin_mode: str，本次尝试设置的保证金模式
        prev_state: tuple[int, str] | None，修改前的 (杠杆, 模式) 状态
        error: Exception，调杠杆时捕获的原始异常
    返回：
        OrderResult | ToolOutcome，对账通过返回下单结果；否则返回提示文案
    """
    logger.warning("调杠杆结果未知，读取持仓对账: %s err=%s", req.contract, error)
    try:
        state = _prev_leverage_state(deps.gateway.list_positions(), req.contract)
    except Exception:
        state = None
    target = (apply_leverage, margin_mode)
    if state == target:  # 远端实际已生效：继续下单
        return await _place_with_rollback(
            deps, req, prev_state, leverage_modified=True, target_state=target
        )
    if state is None:
        await _engage_kill(deps, f"{req.contract} 调杠杆结果未知且无法读取持仓对账")
        return ToolOutcome(
            "调杠杆结果未知且无法读取持仓对账，已开启风控锁，订单未提交，请人工核对杠杆与持仓状态"
        )
    if prev_state is not None and state == prev_state:
        if isinstance(error, GatewayError):
            # 交易所已返回错误响应（明确拒绝），写入确定未执行，可安全宣告未生效
            return ToolOutcome(
                f"调杠杆未生效（{type(error).__name__}: {error}），杠杆仍为修改前状态，"
                "订单未提交，可人工核对后重试"
            )
        # 超时/断连等结果未知：首次读到旧值不能证明写入未生效（可能迟到提交），须延迟复核
        return await _confirm_not_applied_delayed(deps, req, prev_state=prev_state, error=error)
    await _engage_kill(
        deps, f"{req.contract} 调杠杆后状态异常（期望 {target} 或 {prev_state}，实际 {state}）"
    )
    return ToolOutcome(
        f"调杠杆后状态异常：期望 {target[0]}（{target[1]}），实际 {state}，"
        "已开启风控锁，订单未提交，请人工核对"
    )


async def _apply_leverage_and_place(
    deps: ToolDeps,
    req: OrderRequest,
    *,
    apply_leverage: int | None,
    margin_mode: str,
    prev_state: tuple[int, str] | None,
) -> OrderResult | ToolOutcome:
    """按需设置杠杆后下单；目标等于现状跳过设置，结果未知时对账，失败按状态回滚。

    参数：
        deps: ToolDeps，当前模块所需的依赖集合
        req: OrderRequest，订单请求
        apply_leverage: int | None，需要设置的杠杆倍数；None 表示不设置直接下单
        margin_mode: str，设置杠杆时使用的保证金模式
        prev_state: tuple[int, str] | None，修改前的 (杠杆, 模式) 状态，用于失败后回滚
    返回：
        OrderResult | ToolOutcome，成功返回订单结果；明确拒绝、状态未知或异常不明返回提示文案
    """
    if apply_leverage is None or prev_state == (apply_leverage, margin_mode):
        return await _place_with_rollback(deps, req, prev_state, leverage_modified=False)
    try:
        deps.gateway.set_leverage(req.contract, apply_leverage, margin_mode)
    except Exception as e:  # 调杠杆结果未知：先对账再决定（远端可能已生效）
        return await _reconcile_leverage_unknown(
            deps,
            req,
            apply_leverage=apply_leverage,
            margin_mode=margin_mode,
            prev_state=prev_state,
            error=e,
        )
    return await _place_with_rollback(
        deps,
        req,
        prev_state,
        leverage_modified=True,
        target_state=(apply_leverage, margin_mode),
    )
