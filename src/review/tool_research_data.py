"""复盘侧的历史数据回看工具（issue #113 F9）：2 只读。

- read_timeline：回看已读案例窗口内的事实层记录（快讯/日历/指标）；
- get_macro_series：回看已读案例窗口内的 FRED 宏观序列（经研报数据聚合器）。

窗口纪律：两个工具都只允许回看已读案例（get_research_review_case 登记到
deps.loaded_research_cases）的 [created_at, min(window_end, now)] 区间——复盘
只能用"当时已存在"的信息核对依据，越出窗口即拒绝，防止拿案例之后的数据指责
当时判断。read_timeline 直读事实层仓库；get_macro_series 经
deps.research_data_provider（未装配时返回中文降级提示）。
"""

from __future__ import annotations

import math
import time

from src.review.tool_handlers import (
    ReviewToolDeps,
    ToolArgError,
    _clamp,
    _fmt_time,
    _need_str,
    _opt_int,
    _opt_str,
    _to_int,
)


def _need_float(args: dict, name: str) -> float:
    """读取必填的数值参数（Unix 秒时间戳）。

    参数：
        args: dict，工具调用参数字典
        name: str，参数名

    返回：
        float：参数值

    异常：
        ToolArgError：参数缺失或不是数值时抛出
    """
    value = args.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ToolArgError(f"缺少必填参数 {name}（Unix 秒时间戳）")
    return float(value)


def _case_window(deps: ReviewToolDeps, args: dict) -> tuple[float, float] | str:
    """解析 report_id/contract 并返回已读案例的回看窗口 [created_at, upper]。

    参数：
        deps: ReviewToolDeps，复盘工具依赖（读 loaded_research_cases 登记）
        args: dict，工具参数：report_id（必填）、contract（必填）

    返回：
        tuple[float, float] | str：窗口（案例创建时间, min(窗口终点, 当前时间)）；
        未读案例或窗口未知（horizon 非法）时返回中文错误文本
    """
    report_id = _to_int(args.get("report_id"), "report_id")
    contract = _need_str(args, "contract")
    case = deps.loaded_research_cases.get((report_id, contract))
    if case is None:
        return (
            f"参数错误：请先用 get_research_review_case 读取研报#{report_id}/{contract} "
            "的案例材料后再回看窗口数据"
        )
    created_at = case.get("created_at")
    window_end = case.get("window_end")
    if not isinstance(created_at, (int, float)) or not isinstance(window_end, (int, float)):
        return f"参数错误：研报#{report_id}/{contract} 的案例窗口未知（horizon 非法），无法回看"
    return float(created_at), min(float(window_end), time.time())


def _window_error(start_ts: float, end_ts: float, created_at: float, upper: float) -> str | None:
    """校验回看窗口 [start_ts, end_ts] 不越出案例窗口 [created_at, upper]。

    参数：
        start_ts: float，回看起点（Unix 秒）
        end_ts: float，回看终点（Unix 秒）
        created_at: float，案例创建时间（窗口下界）
        upper: float，案例窗口终点与当前时间的较小者（窗口上界）

    返回：
        str | None：越界或倒置时返回中文错误文本，合法返回 None
    """
    if end_ts <= start_ts:
        return "参数错误：end_ts 须大于 start_ts"
    if start_ts < created_at:
        return f"参数错误：start_ts 不得早于案例创建时间（{_fmt_time(created_at)}）"
    if end_ts > upper:
        return (
            f"参数错误：end_ts 不得晚于案例窗口终点与当前时间的较小者（{_fmt_time(upper)}）；"
            "只可用当时已存在的信息核对依据"
        )
    return None


async def read_timeline(deps: ReviewToolDeps, args: dict) -> str:
    """回看已读案例窗口内的事实层记录（按时间正序）。

    参数：
        deps: ReviewToolDeps，复盘工具依赖（经 repo.research.list_timeline 取数）
        args: dict，工具参数：report_id/contract/start_ts/end_ts（必填）、
            kind/keyword（可选过滤）、limit（可选，默认 50，钳制 1~200）

    返回：
        str：事实层记录列表文本；窗口越界或未读案例返回拒绝原因
    """
    window = _case_window(deps, args)
    if isinstance(window, str):
        return window
    created_at, upper = window
    start_ts = _need_float(args, "start_ts")
    end_ts = _need_float(args, "end_ts")
    error = _window_error(start_ts, end_ts, created_at, upper)
    if error is not None:
        return error
    limit = _clamp(_opt_int(args, "limit", 50), 1, 200)
    rows = await deps.repo.research.list_timeline(
        start_ts,
        end_ts,
        limit=limit,
        kind=_opt_str(args, "kind"),
        keyword=_opt_str(args, "keyword"),
    )
    if not rows:
        return f"窗口 [{_fmt_time(start_ts)}, {_fmt_time(end_ts)}) 内无事实层记录"
    lines = [f"事实层记录共 {len(rows)} 条（按时间正序；标题子串/类型过滤已应用）："]
    for row in rows:
        url = f"（{row.url}）" if row.url else ""
        lines.append(
            f"- [{_fmt_time(row.published_at)}] [{row.source}/{row.kind}] {row.title}{url}"
        )
    return "\n".join(lines)


async def get_macro_series(deps: ReviewToolDeps, args: dict) -> str:
    """回看已读案例窗口内的 FRED 宏观序列（供宏观依据的事实核对引用）。

    窗口纪律：end_ts 缺省为 min(案例窗口终点, 当前时间)，显式传入不得晚于它；
    序列起点不早于案例创建时间（look_back 由窗口跨度按天推导）。

    参数：
        deps: ReviewToolDeps，复盘工具依赖（用其 research_data_provider 取 FRED 序列）
        args: dict，工具参数：report_id/contract/indicator（必填）、end_ts（可选）

    返回：
        str：宏观序列文本；未装配数据源、未读案例或窗口越界时返回降级/拒绝原因
    """
    window = _case_window(deps, args)
    if isinstance(window, str):
        return window
    created_at, upper = window
    indicator = _need_str(args, "indicator")
    raw_end = args.get("end_ts")
    if raw_end is not None and (isinstance(raw_end, bool) or not isinstance(raw_end, (int, float))):
        return "参数错误：end_ts 必须是 Unix 秒时间戳"
    end_ts = float(raw_end) if raw_end is not None else upper
    if end_ts > upper:
        return (
            f"参数错误：end_ts 不得晚于案例窗口终点与当前时间的较小者（{_fmt_time(upper)}）；"
            "只可用当时已存在的信息核对依据"
        )
    if end_ts <= created_at:
        return f"参数错误：end_ts 须晚于案例创建时间（{_fmt_time(created_at)}）"
    if deps.research_data_provider is None:
        return "宏观序列数据不可用：研报数据源未装配"
    look_back = max(1, math.ceil((end_ts - created_at) / 86400))
    try:
        return await deps.research_data_provider.get_macro_series(
            indicator, look_back, end_ts=end_ts
        )
    except Exception as exc:  # 数据源失败以降级文本表达，不拖垮本轮复盘
        return f"宏观序列数据不可用：{exc}"
