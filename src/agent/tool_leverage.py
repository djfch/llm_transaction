"""杠杆设置/回滚/对账辅助（place_order 的杠杆安全边界）。

从 tool_trading 拆出以控制单文件体量。核心不变量：
- 声明杠杆下单前，先快照当前 (杠杆, 保证金模式) 作为回滚锚点；
- 交易所明确拒绝（订单确定未创建）才回滚，回滚后必须重读核验；
- 调杠杆异常后读回旧值不能证明写入未执行（GatewayError 统一包装超时/5xx，
  请求可能迟到提交），必须延迟复核确认状态全程稳定后才可宣告"未生效"；
- 回滚失败、核验不一致、订单状态未知、杠杆状态未知，一律触发风控锁（fail closed）。
- 合约级异步锁（ToolDeps.leverage_locks）覆盖"最终重读 → 调杠杆 → 下单前确认 →
  下单 → 失败回滚"整个写事务，进程内改杠杆入口一律经 _locked_leverage_transaction
  复用同一把锁；
- 锁管不了进程外/人工直接改杠杆：set_leverage 成功后下单前必须再读一次确认目标
  状态，读不到即 fail closed（不回滚，避免覆盖第三方状态）；运行约束为"本系统是
  该账户杠杆与保证金状态的单写者"。
- 网关同步 I/O（持仓读取/调杠杆/下单）一律经统一卸载层 run_gateway_io 卸载，
  不得直接阻塞 asyncio 事件循环（关联 issue #72）。
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

from src.agent.tool_handlers import ToolDeps, ToolOutcome
from src.audit.logger import get_logger
from src.gateway.async_io import run_gateway_io
from src.gateway.base import (
    Gateway,
    GatewayError,
    GatewayTransportError,
    OrderRequest,
    OrderResult,
    OrderStateUnknown,
    Position,
)
from src.utils import maybe_await

logger = get_logger(__name__)

# 结果未知（超时/断连）时的延迟复核参数：首次读到旧值不能证明写入未生效
# （请求可能已在服务端排队、随后迟到提交），须轮询确认状态稳定后才可宣告未生效
_UNKNOWN_SETTLE_RETRIES = 3
_UNKNOWN_SETTLE_DELAY_S = 1.0


def _contract_lock(deps: ToolDeps, contract: str) -> asyncio.Lock:
    """取合约级杠杆写事务锁（不存在则创建）；进程内所有改杠杆入口必须复用同一把锁。

    参数：
        deps: ToolDeps，当前模块所需的依赖集合（锁注册表挂在 leverage_locks 字段）
        contract: str，目标合约
    返回：
        asyncio.Lock，该合约的杠杆写事务互斥锁
    """
    return deps.leverage_locks.setdefault(contract, asyncio.Lock())


async def _locked_leverage_transaction(
    deps: ToolDeps,
    req: OrderRequest,
    *,
    prev_state: tuple[int, str] | None,
    verify: bool,
    apply_leverage: int | None,
    margin_mode: str,
    close_epoch: int | None = None,
    reset_epoch: list[int] | None = None,
    reset0: int | None = None,
) -> OrderResult | ToolOutcome:
    """合约级锁内执行杠杆写事务：最终重读 → 调杠杆 → 下单前确认 → 下单 → 失败回滚。

    持锁期间进程内其他改杠杆入口被序列化，重检到下单结束之间不会再有本进程并发写；
    进程外/人工直接改杠杆锁管不了，由锁内重读与下单前确认读兜底（读不到目标态即
    fail closed）。运行约束：本系统是该账户杠杆与保证金状态的单写者。

    参数：
        deps: ToolDeps，当前模块所需的依赖集合
        req: OrderRequest，订单请求
        prev_state: tuple[int, str] | None，风控前捕获的 (杠杆, 模式) 快照
        verify: bool，是否需要锁内重读核验（改杠杆或继承杠杆新增敞口时传 True）
        apply_leverage: int | None，需要设置的杠杆倍数；None 表示不设置直接下单
        margin_mode: str，设置杠杆时使用的保证金模式
        close_epoch: int | None，进入风控流程前捕获的平仓代际（仅增仓单传入）
        reset_epoch: list[int] | None，账户重置代际计数器（ToolDeps.reset_epoch）；
            与 close_epoch 一并传入时，线程内发现账户已重置同样放弃下单（issue #81）
        reset0: int | None，进入风控流程前捕获的重置代际；reset_epoch 非 None 时必填
    返回：
        OrderResult | ToolOutcome，成功返回订单结果；并发变化/明确拒绝/状态未知返回提示文案
    """
    async with _contract_lock(deps, req.contract):
        concurrency_deny = await _recheck_prev_state(deps, req.contract, prev_state, verify=verify)
        if concurrency_deny is not None:
            return concurrency_deny
        return await _apply_leverage_and_place(
            deps,
            req,
            apply_leverage=apply_leverage,
            margin_mode=margin_mode,
            prev_state=prev_state,
            close_epoch=close_epoch,
            reset_epoch=reset_epoch,
            reset0=reset0,
        )


def _prev_leverage_state(
    positions: list[Position], contract: str, *, include_zero: bool = False
) -> tuple[int, str] | None:
    """从持仓快照提取合约当前 (杠杆倍数, 保证金模式)，供失败回滚与对账使用。

    参数：
        positions: list[Position]，当前持仓快照列表
        contract: str，目标合约
        include_zero: bool，是否接受 size=0 的持仓条目；仅"下单前确认 set_leverage
            写入生效"场景传 True（新合约首次调杠杆后仓位尚为零，仍需读出杠杆状态），
            回滚锚点/对账场景保持 False（与既有口径一致）
    返回：
        tuple[int, str] | None，(杠杆倍数, 保证金模式)；无有效持仓（真实网关会返回
        size=0 的历史条目，与 mock/paper 口径统一按无持仓处理），或全仓持仓但
        cross_leverage_limit 缺失/非整数（无可信有效杠杆，非整数如 lever=4.35 回退值
        无法经 int set_leverage 精确回滚）时返回 None
    """
    pos = next(
        (p for p in positions if p.contract == contract and (include_zero or p.size != 0)),
        None,
    )
    if pos is None:
        return None
    if pos.margin_mode == "cross":
        limit = pos.cross_leverage_limit
        if limit is None or limit <= 0 or int(limit) != limit:
            return None
        return max(int(limit), 1), "cross"
    return max(int(pos.leverage), 1), "isolated"


async def _recheck_prev_state(
    deps: ToolDeps, contract: str, prev_state: tuple[int, str] | None, *, verify: bool
) -> ToolOutcome | None:
    """风控 await 窗口后重读杠杆快照：状态被并发修改时触发风控锁并返回拒绝文案。

    参数：
        deps: ToolDeps，当前模块所需的依赖集合
        contract: str，目标合约
        prev_state: tuple[int, str] | None，风控前捕获的 (杠杆, 模式) 快照
        verify: bool，是否需要核验：本调用将修改杠杆，或省略杠杆按快照继承并新增敞口时
            必须传 True（快照值直接参与了风控判定，await 窗口内被改即判定失效）；
            纯平仓/减仓不依赖杠杆快照，传 False 直接跳过

    返回：
        ToolOutcome | None，不需核验或状态一致返回 None；被并发修改返回拒绝文案（已触发风控锁）
    """
    if not verify:
        return None
    latest = _prev_leverage_state(await run_gateway_io(deps.gateway.list_positions), contract)
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
            current = _prev_leverage_state(
                await run_gateway_io(deps.gateway.list_positions), contract
            )
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
        await run_gateway_io(
            deps.gateway.set_leverage, contract, prev_state[0], prev_state[1], mutation=True
        )
    except Exception as e:  # 回滚自身失败：fail closed
        logger.exception("杠杆回滚失败: %s", contract)
        await _engage_kill(deps, f"{contract} 杠杆回滚失败（{type(e).__name__}: {e}）")
        return f"杠杆回滚失败（{type(e).__name__}: {e}），已开启风控锁，请人工核对"
    try:
        state = _prev_leverage_state(await run_gateway_io(deps.gateway.list_positions), contract)
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


def _close_and_bump_epoch(
    gateway: Gateway, req: OrderRequest, epochs: dict[str, int], contract: str
) -> OrderResult:
    """线程内执行平仓并把该合约平仓代际 +1（人工平仓专用，与增仓代际检查构成原子对）。

    真实网关全部写操作在单线程 executor 串行执行，本函数体内无 await：平仓写一旦
    执行，代际必已 +1，后到的增仓代际检查必然读到新值；paper 内联模式在事件循环
    线程同步执行，协程间同样无交错窗口（PR #84 评审 P1）。

    代际语义是"人工平仓意图已介入"，不只表达"客户端收到成功回执"：状态未知
    （OrderStateUnknown）、传输层异常（GatewayTransportError，超时/重试耗尽，
    请求可能已到达交易所）与非网关异常同样上调代际——远端可能已平仓，旧代际的
    增仓写必须中止；只有交易所带 label 的明确拒绝（其余 GatewayError）可证明
    平仓未发生，不上调（PR #84 评审 P1：状态未知不递增代际则旧增仓可重开仓）。

    参数：
        gateway: Gateway，交易网关
        req: OrderRequest，平仓订单请求
        epochs: dict[str, int]，合约级平仓代际表（ToolDeps.close_epochs）
        contract: str，目标合约
    返回：
        OrderResult，平仓订单结果
    异常：
        GatewayError，下单被明确拒绝或状态未知时原样上抛（状态未知已先上调代际）
    """
    try:
        result = gateway.place_order(req)
    except Exception as e:
        # 仅交易所明确拒绝（非状态未知/传输未知的 GatewayError）可证明平仓未发生；
        # 其余一律视为意图已介入：先上调代际再上抛，旧增仓写将被代际比对拦下
        if not isinstance(e, GatewayError) or isinstance(
            e, (OrderStateUnknown, GatewayTransportError)
        ):
            epochs[contract] = epochs.get(contract, 0) + 1
        raise
    epochs[contract] = epochs.get(contract, 0) + 1
    return result


def _place_unless_close_intervened(
    gateway: Gateway,
    req: OrderRequest,
    epochs: dict[str, int],
    contract: str,
    epoch0: int,
    resets: list[int] | None = None,
    reset0: int | None = None,
) -> OrderResult | None:
    """线程内比对平仓/重置代际：人工平仓或账户重置介入则放弃本次增仓，否则下单。

    检查与下单同在单线程 executor 的一个任务内（或 paper 内联模式的同步段内），
    人工平仓写要么排在检查之前（代际已变，放弃下单）、要么排在本单之后
    （先开后平，最终仍为空仓），不存在"平完又被旧写重开"的乱序。

    参数：
        gateway: Gateway，交易网关
        req: OrderRequest，增仓订单请求
        epochs: dict[str, int]，合约级平仓代际表（ToolDeps.close_epochs）
        contract: str，目标合约
        epoch0: int，进入风控流程前捕获的平仓代际
        resets: list[int] | None，账户重置代际计数器（ToolDeps.reset_epoch）；
            None 表示不校验重置代际
        reset0: int | None，进入风控流程前捕获的重置代际；resets 非 None 时必填
    返回：
        OrderResult | None，代际一致返回下单结果；任一代际已变（人工平仓介入或
        账户已重置）返回 None
    """
    if epochs.get(contract, 0) != epoch0:
        return None
    if resets is not None and resets[0] != reset0:
        return None
    return gateway.place_order(req)


def _amend_unless_close_intervened(
    gateway: Gateway,
    contract: str,
    order_id: str,
    price: Decimal | None,
    size: Decimal | None,
    epochs: dict[str, int],
    epoch0: int,
    resets: list[int] | None = None,
    reset0: int | None = None,
    expected_position_size: Decimal | None = None,
) -> OrderResult | None:
    """线程内比对平仓/重置代际：人工平仓或账户重置介入则放弃增仓改单，否则执行改单。

    与 _place_unless_close_intervened 同一不变量：检查与改单同在单线程 executor
    的一个任务内（或 paper 内联同步段），人工平仓写要么排在检查之前（代际已变，
    放弃改单）、要么排在本单之后（先改后平，挂单不再成交）；防止高优人工平仓
    插入风控窗口后，旧增仓改单把挂单改大/改成可成交价再次开仓（PR #84 评审 P1：
    代际机制只封新建订单，没封把已有订单修改成增仓的同类写路径）。

    参数：
        gateway: Gateway，交易网关
        contract: str，合约标识
        order_id: str，待修改的订单 ID
        price: Decimal | None，改后价格（None 表示不改）
        size: Decimal | None，改后张数（None 表示不改）
        epochs: dict[str, int]，合约级平仓代际表（ToolDeps.close_epochs）
        epoch0: int，进入改单流程时捕获的平仓代际
        resets: list[int] | None，账户重置代际计数器（ToolDeps.reset_epoch）；
            None 表示不校验重置代际
        reset0: int | None，进入改单流程前捕获的重置代际；resets 非 None 时必填
        expected_position_size: Decimal | None，风控校验时的持仓张数；提供时须在改单前一致
    返回：
        OrderResult | None，代际一致返回改单结果；任一代际已变（人工平仓介入或
        账户已重置）返回 None
    """
    if epochs.get(contract, 0) != epoch0:
        return None
    if resets is not None and resets[0] != reset0:
        return None
    if expected_position_size is not None:
        position = next(
            (item for item in gateway.list_positions() if item.contract == contract), None
        )
        actual_size = position.size if position is not None else Decimal(0)
        if actual_size != expected_position_size:
            return None
    return gateway.amend_order(contract, order_id, price=price, size=size)


async def _place_with_rollback(
    deps: ToolDeps,
    req: OrderRequest,
    prev_state: tuple[int, str] | None,
    *,
    leverage_modified: bool,
    target_state: tuple[int, str] | None = None,
    close_epoch: int | None = None,
    reset_epoch: list[int] | None = None,
    reset0: int | None = None,
) -> OrderResult | ToolOutcome:
    """杠杆就位后下单；被拒时回滚（仅当改过杠杆），状态未知或不明确异常时触发风控锁。

    参数：
        deps: ToolDeps，当前模块所需的依赖集合
        req: OrderRequest，订单请求
        prev_state: tuple[int, str] | None，修改前的 (杠杆, 模式) 状态，用于失败后回滚
        leverage_modified: bool，本次调用是否实际修改过杠杆（未修改则失败无需回滚）
        target_state: tuple[int, str] | None，本调用设置的目标 (杠杆, 模式)，回滚前 CAS 核验用
        close_epoch: int | None，进入风控流程前捕获的平仓代际；仅增仓单传入，
            executor 线程内发现人工平仓介入即放弃下单（中止不做杠杆回滚：
            仓位已被人工处置，盲写旧杠杆快照反而会干扰人工后续操作）
        reset_epoch: list[int] | None，账户重置代际计数器（ToolDeps.reset_epoch）；
            与 close_epoch 一并传入时，线程内发现账户已重置同样放弃下单（issue #81）
        reset0: int | None，进入风控流程前捕获的重置代际；reset_epoch 非 None 时必填
    返回：
        OrderResult | ToolOutcome，成功返回订单结果；明确拒绝、状态未知或异常不明返回提示文案
    """
    try:
        if close_epoch is None:
            return await run_gateway_io(deps.gateway.place_order, req, mutation=True)
        placed = await run_gateway_io(
            _place_unless_close_intervened,
            deps.gateway,
            req,
            deps.close_epochs,
            req.contract,
            close_epoch,
            resets=reset_epoch,
            reset0=reset0,
            mutation=True,
        )
        if placed is None:
            return ToolOutcome(
                f"已中止：{req.contract} 在风控校验期间被人工平仓或账户重置，"
                "本次增仓订单未提交，请重新评估",
                "deny",
                "人工平仓介入",
            )
        return placed
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
            state = _prev_leverage_state(
                await run_gateway_io(deps.gateway.list_positions), req.contract
            )
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
    close_epoch: int | None = None,
    reset_epoch: list[int] | None = None,
    reset0: int | None = None,
) -> OrderResult | ToolOutcome:
    """调杠杆结果未知时读取持仓对账：已达目标继续下单，其余按异常类别分流。

    读回旧值不等于写入未执行：GatewayError 是网关层统一包装（超时/5xx 同样落入
    该类别），请求可能在服务端迟到提交，一律进入延迟复核，确认状态全程稳定后
    才可宣告未生效。

    参数：
        deps: ToolDeps，当前模块所需的依赖集合
        req: OrderRequest，订单请求
        apply_leverage: int，本次尝试设置的杠杆倍数
        margin_mode: str，本次尝试设置的保证金模式
        prev_state: tuple[int, str] | None，修改前的 (杠杆, 模式) 状态
        error: Exception，调杠杆时捕获的原始异常
        close_epoch: int | None，进入风控流程前捕获的平仓代际（仅增仓单传入）
        reset_epoch: list[int] | None，账户重置代际计数器（ToolDeps.reset_epoch）
        reset0: int | None，进入风控流程前捕获的重置代际；reset_epoch 非 None 时必填
    返回：
        OrderResult | ToolOutcome，对账通过返回下单结果；否则返回提示文案
    """
    logger.warning("调杠杆结果未知，读取持仓对账: %s err=%s", req.contract, error)
    try:
        state = _prev_leverage_state(
            await run_gateway_io(deps.gateway.list_positions), req.contract
        )
    except Exception:
        state = None
    target = (apply_leverage, margin_mode)
    if state == target:  # 远端实际已生效：继续下单
        return await _place_with_rollback(
            deps,
            req,
            prev_state,
            leverage_modified=True,
            target_state=target,
            close_epoch=close_epoch,
            reset_epoch=reset_epoch,
            reset0=reset0,
        )
    if state is None:
        await _engage_kill(deps, f"{req.contract} 调杠杆结果未知且无法读取持仓对账")
        return ToolOutcome(
            "调杠杆结果未知且无法读取持仓对账，已开启风控锁，订单未提交，请人工核对杠杆与持仓状态"
        )
    if prev_state is not None and state == prev_state:
        # 读回旧值不能证明写入未执行：GatewayError 是网关层统一包装（含超时/5xx 等
        # 结果未知场景），请求可能迟到提交，一律延迟复核确认状态稳定
        return await _confirm_not_applied_delayed(deps, req, prev_state=prev_state, error=error)
    await _engage_kill(
        deps, f"{req.contract} 调杠杆后状态异常（期望 {target} 或 {prev_state}，实际 {state}）"
    )
    return ToolOutcome(
        f"调杠杆后状态异常：期望 {target[0]}（{target[1]}），实际 {state}，"
        "已开启风控锁，订单未提交，请人工核对"
    )


async def _confirm_target_applied(
    deps: ToolDeps, contract: str, target: tuple[int, str], *, receipt: Position
) -> ToolOutcome | None:
    """set_leverage 成功后、下单前重读确认目标状态仍就位；读不到目标态即 fail closed。

    锁只能序列化进程内写入，进程外/人工直接改杠杆可能落在调杠杆与下单之间：确认读
    读到非目标态（含读取失败）时不回滚（状态可能被第三方占用，盲写旧快照会覆盖他人
    修改），保持现状并触发风控锁。新合约首次调杠杆后仓位为零，部分网关口径的
    list_positions 不返回零仓条目，此时回退核验写入回执（set_leverage 返回值）。

    参数：
        deps: ToolDeps，当前模块所需的依赖集合
        contract: str，目标合约
        target: tuple[int, str]，本调用设置的目标 (杠杆, 模式)
        receipt: Position，set_leverage 的写入回执（持仓快照返回值）
    返回：
        ToolOutcome | None，状态为目标态返回 None；否则返回拒绝文案（已触发风控锁）
    """
    try:
        # include_zero=True：新合约首次调杠杆后仓位尚为零，仍需读出刚写入的杠杆状态
        confirmed = _prev_leverage_state(
            await run_gateway_io(deps.gateway.list_positions), contract, include_zero=True
        )
    except Exception:
        confirmed = None
    if confirmed is None:  # 零仓条目被网关列表过滤：回退核验写入回执
        confirmed = _prev_leverage_state([receipt], contract, include_zero=True)
    if confirmed == target:
        return None
    await _engage_kill(
        deps, f"{contract} 下单前杠杆目标状态核验异常（期望 {target}，实际 {confirmed}）"
    )
    return ToolOutcome(
        f"下单前杠杆状态核验异常：期望 {target[0]}（{target[1]}），实际 {confirmed}，"
        "存在并发修改，已开启风控锁，订单未提交，请人工核对",
        "deny",
        "杠杆状态并发变化",
    )


async def _apply_leverage_and_place(
    deps: ToolDeps,
    req: OrderRequest,
    *,
    apply_leverage: int | None,
    margin_mode: str,
    prev_state: tuple[int, str] | None,
    close_epoch: int | None = None,
    reset_epoch: list[int] | None = None,
    reset0: int | None = None,
) -> OrderResult | ToolOutcome:
    """按需设置杠杆后下单；目标等于现状跳过设置，结果未知时对账，下单前确认目标状态。

    参数：
        deps: ToolDeps，当前模块所需的依赖集合
        req: OrderRequest，订单请求
        apply_leverage: int | None，需要设置的杠杆倍数；None 表示不设置直接下单
        margin_mode: str，设置杠杆时使用的保证金模式
        prev_state: tuple[int, str] | None，修改前的 (杠杆, 模式) 状态，用于失败后回滚
        close_epoch: int | None，进入风控流程前捕获的平仓代际（仅增仓单传入）
        reset_epoch: list[int] | None，账户重置代际计数器（ToolDeps.reset_epoch）
        reset0: int | None，进入风控流程前捕获的重置代际；reset_epoch 非 None 时必填
    返回：
        OrderResult | ToolOutcome，成功返回订单结果；明确拒绝、状态未知或异常不明返回提示文案
    """
    if apply_leverage is None or prev_state == (apply_leverage, margin_mode):
        return await _place_with_rollback(
            deps,
            req,
            prev_state,
            leverage_modified=False,
            close_epoch=close_epoch,
            reset_epoch=reset_epoch,
            reset0=reset0,
        )
    try:
        receipt = await run_gateway_io(
            deps.gateway.set_leverage, req.contract, apply_leverage, margin_mode, mutation=True
        )
    except Exception as e:  # 调杠杆结果未知：先对账再决定（远端可能已生效）
        return await _reconcile_leverage_unknown(
            deps,
            req,
            apply_leverage=apply_leverage,
            margin_mode=margin_mode,
            prev_state=prev_state,
            error=e,
            close_epoch=close_epoch,
            reset_epoch=reset_epoch,
            reset0=reset0,
        )
    target = (apply_leverage, margin_mode)
    # 下单前确认目标状态：锁管不了进程外修改，读不到目标态即 fail closed（不回滚）
    confirm_deny = await _confirm_target_applied(deps, req.contract, target, receipt=receipt)
    if confirm_deny is not None:
        return confirm_deny
    return await _place_with_rollback(
        deps,
        req,
        prev_state,
        leverage_modified=True,
        target_state=target,
        close_epoch=close_epoch,
        reset_epoch=reset_epoch,
        reset0=reset0,
    )
