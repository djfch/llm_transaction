"""非交易类 LLM 工具的异步执行函数 + 共享基础设施（ToolDeps/校验辅助）。

约定：
- 参数校验失败抛 ToolArgError，由 ToolRegistry 统一转成错误文本（不向上抛异常）
- 交易类工具（place_order / update_tpsl / amend_order / cancel_order）在 tool_trading，
  内部先过风控，拒绝则返回理由文本
- 金额/数量一律 Decimal；返回 ToolOutcome 携带风控判定（供审计落库）
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from src.config import ResearchConfig, RiskConfig
from src.gateway.base import Gateway
from src.market.candles import CandleCache
from src.market.indicator_service import IndicatorService
from src.market.intervals import GATE_CANDLE_INTERVALS, interval_seconds
from src.market.triggers import MAX_ALERTS, TriggerManager
from src.memory.repo import Repo
from src.risk.engine import RiskEngine
from src.risk.models import DailyStats
from src.utils import calc_expression, fmt_indicator_value


class ToolArgError(Exception):
    """工具参数校验失败（转错误文本返回给 LLM，不中断本轮）。"""


@dataclass
class ToolOutcome:
    """工具执行结果。risk_verdict: allow/deny/空串（非风控工具）。"""

    text: str
    risk_verdict: str = ""
    risk_reason: str = ""


DailyStatsFn = Callable[[], Awaitable[DailyStats]]


@dataclass
class ToolDeps:
    """工具执行所需依赖（round_id 由决策循环每轮写入）。"""

    gateway: Gateway
    risk_engine: RiskEngine
    risk_config: RiskConfig
    watchlist: list[str]
    repo: Repo
    candles: CandleCache
    triggers: TriggerManager
    indicator_service: IndicatorService | None  # None=未接入：get_indicators 如实回报不可用
    daily_stats_fn: DailyStatsFn
    research_config: ResearchConfig | None = None  # 研报方向闸门配置（None=闸门关闭）
    mode: str = "paper"
    set_next_wake: Callable[[int], int] | None = None  # 返回钳制后实际生效分钟数
    notify_event: Callable[[dict], None] | None = None  # WS 事件广播（如 plan_updated）
    round_id: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


# ---------- 参数校验辅助 ----------


def _need_str(args: dict, name: str) -> str:
    """从 LLM 工具参数中取出必填字符串，去首尾空白后返回。

    参数：
        args: dict，LLM 传入的工具参数
        name: str，参数名

    返回：
        str：去空白后的非空参数值

    异常：
        ToolArgError：参数缺失、不是字符串或去空白后为空时抛出
    """
    v = args.get(name)
    if not isinstance(v, str) or not v.strip():
        raise ToolArgError(f"缺少必填参数 {name}（非空字符串）")
    return v.strip()


def _to_decimal(v: Any, name: str) -> Decimal:
    """把单个参数值转换为 Decimal，布尔值与非数字类型一律拒绝。

    参数：
        v: Any，待转换的值（接受 int/float/str/Decimal，拒绝 bool）
        name: str，参数名（用于错误提示文案）

    返回：
        Decimal：转换后的数值

    异常：
        ToolArgError：值类型不支持或无法解析为数字时抛出
    """
    if isinstance(v, bool) or not isinstance(v, (int, float, str, Decimal)):
        raise ToolArgError(f"参数 {name} 必须是数字")
    try:
        return Decimal(str(v))
    except InvalidOperation as e:
        raise ToolArgError(f"参数 {name} 必须是数字") from e


def _need_decimal(args: dict, name: str) -> Decimal:
    """从 LLM 工具参数中取出必填数字并转换为 Decimal。

    参数：
        args: dict，LLM 传入的工具参数
        name: str，参数名

    返回：
        Decimal：参数的数值

    异常：
        ToolArgError：参数缺失或为 None 时抛出
    """
    if name not in args or args[name] is None:
        raise ToolArgError(f"缺少必填参数 {name}（数字）")
    return _to_decimal(args[name], name)


def _opt_decimal(args: dict, name: str) -> Decimal | None:
    """从 LLM 工具参数中取出可选数字并转换为 Decimal；未传时返回 None。

    参数：
        args: dict，LLM 传入的工具参数
        name: str，参数名

    返回：
        Decimal | None：参数的数值；参数缺失或为 None 时返回 None
    """
    return None if args.get(name) is None else _to_decimal(args[name], name)


def _need_int(args: dict, name: str) -> int:
    """从 LLM 工具参数中取出必填整数。

    参数：
        args: dict，LLM 传入的工具参数
        name: str，参数名

    返回：
        int：参数的整数值

    异常：
        ToolArgError：参数缺失、为 None/布尔值或无法转为整数时抛出
    """
    v = args.get(name)
    if v is None or isinstance(v, bool):
        raise ToolArgError(f"缺少必填参数 {name}（整数）")
    try:
        return int(v)
    except (TypeError, ValueError) as e:
        raise ToolArgError(f"参数 {name} 必须是整数") from e


def _opt_int(args: dict, name: str, default: int) -> int:
    """从 LLM 工具参数中取出可选整数；未传时使用默认值。

    参数：
        args: dict，LLM 传入的工具参数
        name: str，参数名
        default: int，参数缺失或为 None 时采用的默认值

    返回：
        int：参数的整数值；未传时为 default
    """
    return default if args.get(name) is None else _need_int(args, name)


def _opt_bool(args: dict, name: str, default: bool = False) -> bool:
    """从 LLM 工具参数中取出可选布尔值；只接受 JSON 布尔，未传时使用默认值。

    参数：
        args: dict，LLM 传入的工具参数
        name: str，参数名
        default: bool，参数缺失或为 None 时采用的默认值

    返回：
        bool：参数值；未传时为 default

    异常：
        ToolArgError：参数不是布尔类型（字符串、0/1 整数等均拒绝）时抛出
    """
    v = args.get(name)
    if v is None:
        return default
    if not isinstance(v, bool):
        raise ToolArgError(f"参数 {name} 必须是布尔值 true/false")
    return v


def _opt_enum(args: dict, name: str, options: set[str]) -> str | None:
    """从 LLM 工具参数中取出可选枚举并校验取值合法；未传时返回 None。

    参数：
        args: dict，LLM 传入的工具参数
        name: str，参数名
        options: set[str]，合法取值集合

    返回：
        str | None：参数值；未传该参数时为 None

    异常：
        ToolArgError：参数不是字符串或取值不在合法集合内时抛出
    """
    v = args.get(name)
    if v is None:
        return None
    if not isinstance(v, str) or v not in options:
        raise ToolArgError(f"参数 {name} 取值非法：{v!r}（可选 {'/'.join(sorted(options))}）")
    return v


def _need_enum(args: dict, name: str, options: set[str]) -> str:
    """从 LLM 工具参数中取出必填枚举并校验取值合法。

    参数：
        args: dict，LLM 传入的工具参数
        name: str，参数名
        options: set[str]，合法取值集合

    返回：
        str：参数值

    异常：
        ToolArgError：参数缺失或取值不在合法集合内时抛出
    """
    v = _opt_enum(args, name, options)
    if v is None:
        raise ToolArgError(f"缺少必填参数 {name}（可选 {'/'.join(sorted(options))}）")
    return v


def _clamp(v: int, lo: int, hi: int) -> int:
    """把整数钳制到指定闭区间内。

    参数：
        v: int，待钳制的值
        lo: int，区间下限
        hi: int，区间上限

    返回：
        int：钳制后的值（小于下限取下限，大于上限取上限）
    """
    return max(lo, min(hi, v))


# ---------- 工具执行函数 ----------


async def get_market_data(deps: ToolDeps, args: dict) -> ToolOutcome:
    """读取合约最近若干根 K 线，排版为北京时间逐行文本供 LLM 阅读。

    最后一根尚未收盘的形成中 K 线会标注「（未收盘）」；无数据时如实返回「暂无 K 线数据」。

    参数：
        deps: ToolDeps，工具依赖（使用其中的 candles K 线缓存）
        args: dict，工具参数：contract 合约名（必填）；interval K 线周期（可选，默认 1h）；
            limit 根数（可选，默认 24，钳制到 1–100）

    返回：
        ToolOutcome：K 线逐行文本（时间/开/收/高/低/量），不携带风控判定
    """
    contract = _need_str(args, "contract")
    interval = _opt_enum(args, "interval", set(GATE_CANDLE_INTERVALS)) or "1h"
    limit = _clamp(_opt_int(args, "limit", 24), 1, 100)
    candles = deps.candles.get_recent(contract, interval, limit)
    lines = [
        f"交易对：{contract}；时间尺度：{interval}；时间：北京时间（UTC+8）",
        "时间（年月日时分） | 开盘价 | 收盘价 | 最高价格 | 最低价格 | 交易量",
    ]
    if not candles:
        lines.append("暂无 K 线数据")
        return ToolOutcome("\n".join(lines))
    # 最后一根通常是窗口未结束的形成中 K 线（OHLCV 为截至当前的累计快照）：按
    # 开盘时间 + 周期秒数 > 当前时刻判定并标注，避免把不完整的量/涨跌幅误读成缩量
    zone = timezone(timedelta(hours=8), name="UTC+8")
    span = interval_seconds(interval)
    now = time.time()
    for candle in candles:
        timestamp = datetime.fromtimestamp(candle.t, zone).strftime("%Y-%m-%d %H:%M")
        suffix = " （未收盘）" if candle.t + span > now else ""
        lines.append(
            f"{timestamp} | {candle.o} | {candle.c} | {candle.h} | {candle.l} | {candle.v}{suffix}"
        )
    return ToolOutcome("\n".join(lines))


async def get_indicators(deps: ToolDeps, args: dict) -> ToolOutcome:
    """全部技术指标当前值（中文逐行文本）；指标服务异常转错误文本，不向上抛。

    参数：
        deps: ToolDeps，当前模块所需的依赖集合
        args: dict，工具调用参数
    返回：
        ToolOutcome，全部技术指标当前值（中文逐行文本）；指标服务异常转错误文本，不向上抛
    异常：
        ToolArgError，请求合约不在交易白名单时抛出
    """
    contract = _need_str(args, "contract")
    if contract not in deps.watchlist:
        raise ToolArgError(f"合约 {contract} 不在白名单（当前白名单：{', '.join(deps.watchlist)}）")
    interval = _opt_enum(args, "interval", set(GATE_CANDLE_INTERVALS)) or "1h"
    if deps.indicator_service is None:
        return ToolOutcome("错误：指标服务未接入，暂无法获取技术指标")
    try:
        panel = deps.indicator_service.full_panel(contract, interval)
    except Exception as e:  # 服务未就绪/缓存异常：如实回报，不拖垮本轮决策
        return ToolOutcome(f"错误：指标计算失败（{type(e).__name__}: {e}）")
    lines = [f"{contract} 技术指标（{interval}，按指标各自所需深度计算）:"]
    for item in panel["indicators"].values():
        rendered = {k: fmt_indicator_value(v) for k, v in item["values"].items()}
        if all(v == "无数据" for v in rendered.values()):
            lines.append(f"{item['label']}: 无数据")
        elif len(rendered) == 1:
            lines.append(f"{item['label']}: {next(iter(rendered.values()))}")
        else:  # 多值指标一行列子字段；个别字段缺失如实标 无数据
            fields = ", ".join(f"{k}={v}" for k, v in rendered.items())
            lines.append(f"{item['label']}: {fields}")
    return ToolOutcome("\n".join(lines))


async def set_price_alert(deps: ToolDeps, args: dict) -> ToolOutcome:
    """创建价格预警触发器；相同预警已存在或数量达上限时不创建并如实回报。

    参数：
        deps: ToolDeps，工具依赖（使用其中的 triggers 触发器管理器）
        args: dict，工具参数：contract 合约名、direction 方向（above/below）、price 触发价，
            三者均必填

    返回：
        ToolOutcome：结果文本（含触发器 id）；重复或超限时为说明文本，未做修改

    异常：
        ToolArgError：price 不是正数时抛出
    """
    contract = _need_str(args, "contract")
    direction = _need_enum(args, "direction", {"above", "below"})
    price = _need_decimal(args, "price")
    if price <= 0:
        raise ToolArgError("price 必须为正数")
    sym = ">=" if direction == "above" else "<="
    # 相同 (contract, direction, price) 查重：如实告知已设置，不重复创建（内存唯一存储）
    existing = deps.triggers.find(contract, sym, price)
    if existing is not None:
        return ToolOutcome(
            f"该价格预警已设置：{contract} {direction} {price}（触发器 {existing.id}），无需重复创建"
        )
    # 全局上限预检：达标时拒绝创建，错误文本回给 LLM（TriggerManager.add 另有硬校验兜底）
    if len(deps.triggers.list()) >= MAX_ALERTS:
        return ToolOutcome(
            f"错误：价格预警数量已达上限（{MAX_ALERTS} 条），未创建新预警；"
            "请先用 cancel_price_alert 取消不需要的预警线后再设置"
        )
    trigger = deps.triggers.add(contract, sym, price)
    return ToolOutcome(f"已设置价格预警：{contract} {direction} {price}（触发器 {trigger.id}）")


async def cancel_price_alert(deps: ToolDeps, args: dict) -> ToolOutcome:
    """按合约、方向、价格删除已设置的价格预警；找不到时不做任何修改。

    参数：
        deps: ToolDeps，工具依赖（使用其中的 triggers 触发器管理器）
        args: dict，工具参数：contract 合约名、direction 方向（above/below）、price 触发价，
            三者均必填

    返回：
        ToolOutcome：结果文本；无相同预警线时为说明文本，未做修改

    异常：
        ToolArgError：price 不是正数时抛出
    """
    contract = _need_str(args, "contract")
    direction = _need_enum(args, "direction", {"above", "below"})
    price = _need_decimal(args, "price")
    if price <= 0:
        raise ToolArgError("price 必须为正数")
    sym = ">=" if direction == "above" else "<="
    existing = deps.triggers.find(contract, sym, price)
    if existing is None:
        return ToolOutcome(
            f"未找到该价格预警线：{contract} {direction} {price}（当前无相同预警线，未做任何修改）"
        )
    deps.triggers.remove(existing.id)
    return ToolOutcome(f"已取消价格预警：{contract} {direction} {price}（触发器 {existing.id}）")


async def set_next_wakeup(deps: ToolDeps, args: dict) -> ToolOutcome:
    """设置决策循环下次唤醒的分钟数，回报钳制后的实际生效值。

    参数：
        deps: ToolDeps，工具依赖（使用其中的 set_next_wake 调度回调，None 表示调度器未接入）
        args: dict，工具参数：minutes 几分钟后唤醒（必填，正整数）

    返回：
        ToolOutcome：结果文本（含实际生效分钟数）；调度器未接入时为错误文本

    异常：
        ToolArgError：minutes 不是正整数时抛出
    """
    minutes = _need_int(args, "minutes")
    if minutes <= 0:
        raise ToolArgError("minutes 必须为正整数")
    if deps.set_next_wake is None:
        return ToolOutcome("错误：调度器未接入，无法设置下次唤醒")
    effective = deps.set_next_wake(minutes)
    return ToolOutcome(f"下次唤醒已设置为 {effective} 分钟后（请求 {minutes} 分钟）")


async def write_note(deps: ToolDeps, args: dict) -> ToolOutcome:
    """把一条笔记写入数据库，归属当前决策轮次。

    参数：
        deps: ToolDeps，工具依赖（使用其中的 repo 存储与 round_id 当前轮次标识）
        args: dict，工具参数：content 笔记内容（必填，非空字符串）

    返回：
        ToolOutcome：结果文本（含新笔记 id）
    """
    content = _need_str(args, "content")
    note = await deps.repo.add_note(deps.round_id, content)
    return ToolOutcome(f"笔记已保存（id={note.id}）")


async def calc(deps: ToolDeps, args: dict) -> ToolOutcome:
    """数学表达式计算（纯函数，不碰任何依赖）；错误以中文文本返回。

    参数：
        deps: ToolDeps，当前模块所需的依赖集合
        args: dict，工具调用参数
    返回：
        ToolOutcome，数学表达式计算（纯函数，不碰任何依赖）；错误以中文文本返回
    """
    expression = _need_str(args, "expression")
    return ToolOutcome(calc_expression(expression))


async def get_history(deps: ToolDeps, args: dict) -> ToolOutcome:
    """汇总最近的成交记录与决策轮次，排版为逐行文本供 LLM 回顾。

    参数：
        deps: ToolDeps，工具依赖（使用其中的 repo 存储）
        args: dict，工具参数：limit 成交笔数上限（可选，默认 20，钳制到 1–50；
            决策轮次最多取 10 条）

    返回：
        ToolOutcome：近 N 笔成交与近 M 轮决策的摘要文本
    """
    limit = _clamp(_opt_int(args, "limit", 20), 1, 50)
    trades = (await deps.repo.trades_between(0.0, time.time()))[-limit:]
    decisions = await deps.repo.list_decisions(limit=min(limit, 10))
    lines = [f"近 {len(trades)} 笔成交："]
    lines += [f"- {t.contract} size={t.size} 价格 {t.price}，盈亏 {t.pnl}" for t in trades]
    lines.append(f"近 {len(decisions)} 轮决策：")
    lines += [
        f"- round={d.round_id[:8]} 唤醒={d.wake_source} "
        f"时间={time.strftime('%m-%d %H:%M', time.localtime(d.created_at))}"
        for d in decisions
    ]
    return ToolOutcome("\n".join(lines))
