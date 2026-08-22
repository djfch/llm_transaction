"""复盘 agent 的指标相关工具：指标面板只读查询 + 指标短名单版本化改写。

指标相关工具独立维护以控制体量（tool_handlers.py 接近 300 行红线）：
- get_indicators：渲染 IndicatorService.full_panel 为逐行中文文本（None 显示 无数据）；
- get_indicator_config：当前生效短名单 + 可选指标全集菜单；
- submit_indicator_config：短名单改写写出口（校验拒绝转中文文本返回，不向上抛）。

依赖经 ReviewToolDeps 注入；indicator_service / indicator_config_store 为 None
（未装配）时返回「指标功能未配置」中文降级提示，不抛异常。
允许 import src/market/*（基础设施层）；本包红线不变：不 import src/agent/*。
"""

from __future__ import annotations

from src.market.indicator_service import REGISTRY
from src.market.intervals import GATE_CANDLE_INTERVALS
from src.review.indicator_config import IndicatorConfigValidationError
from src.review.tool_handlers import ReviewToolDeps, ToolArgError, _fmt_time, _need_str
from src.utils import fmt_indicator_value

_DEFAULT_INTERVAL = "1h"  # 缺省 K 线周期，与执行 agent 工具惯例一致

_KIND_ZH = {"overlay": "主图叠加", "pane": "副图", "scalar": "单值"}  # kind 分组中文释义

_SERVICE_MISSING = "指标功能未配置（indicator_service 未注入，装配后可使用）"
_STORE_MISSING = "指标功能未配置（indicator_config_store 未注入，装配后可使用）"


def indicator_menu_items() -> list[str]:
    """可选指标全集菜单条目（key=label/中文kind）；schema 描述与工具输出共用同一数据源。

    参数：无

    返回：
        list[str]，可选指标全集菜单条目（key=label/中文kind）；schema 描述与工具输出共用同一数据源
    """
    return [f"{key}={defn.label}/{_KIND_ZH[defn.kind]}" for key, defn in REGISTRY.items()]


# ---------- 参数校验辅助 ----------


def _opt_interval(args: dict) -> str:
    """可选 K 线周期：缺省 '1h'；非法值抛 ToolArgError（由注册表转中文错误文本）。

    参数：
        args: dict，工具调用参数

    返回：
        str，可选 K 线周期：缺省 '1h'；非法值抛 ToolArgError（由注册表转中文错误文本）

    异常：
        ToolArgError，interval 不在 Gate 支持的 K 线周期中时抛出
    """
    v = args.get("interval")
    if v is None:
        return _DEFAULT_INTERVAL
    if not isinstance(v, str) or v not in GATE_CANDLE_INTERVALS:
        raise ToolArgError(
            f"参数 interval 取值非法：{v!r}（可选 {'/'.join(GATE_CANDLE_INTERVALS)}）"
        )
    return v


def _need_str_list(args: dict, name: str) -> list[str]:
    """必填非空字符串数组（元素 strip 后返回）；形状非法抛 ToolArgError。

    参数：
        args: dict，工具调用参数
        name: str，工具名或参数名

    返回：
        list[str]，必填非空字符串数组（元素 strip 后返回）；形状非法抛 ToolArgError

    异常：
        ToolArgError，参数不是非空列表或任一元素不是非空字符串时抛出
    """
    v = args.get(name)
    if not isinstance(v, list) or not v:
        raise ToolArgError(f"缺少必填参数 {name}（非空字符串数组）")
    if any(not isinstance(i, str) or not i.strip() for i in v):
        raise ToolArgError(f"参数 {name} 的每个元素必须是非空字符串")
    return [i.strip() for i in v]


# ---------- 排版辅助 ----------


def _render_values(item: dict) -> str:
    """单指标值文本：单字段只给值；多字段 field=value 逐一列出（None 显示 无数据）。

    参数：
        item: dict，提供商响应中的工具调用项

    返回：
        str，单指标值文本：单字段只给值；多字段 field=value 逐一列出（None 显示 无数据）
    """
    rendered = {k: fmt_indicator_value(v) for k, v in item["values"].items()}
    if len(rendered) == 1:
        return next(iter(rendered.values()))
    return "，".join(f"{field}={v}" for field, v in rendered.items())


def _render_panel(panel: dict) -> str:
    """full_panel 字典 → 逐行中文文本（每指标一行：key | label：值）。

    参数：
        panel: dict，完整指标面板字典

    返回：
        str，full_panel 字典 → 逐行中文文本（每指标一行：key | label：值）
    """
    ts = panel["time"]
    head = f"{panel['contract']} 指标面板（{panel['interval']}）"
    head += "：无K线数据" if ts is None else f"：截至 {_fmt_time(ts)}"
    lines = [head]
    for key, item in panel["indicators"].items():
        lines.append(f"- {key} | {item['label']}：{_render_values(item)}")
    return "\n".join(lines)


# ---------- 只读工具 ----------


async def get_indicators(deps: ReviewToolDeps, args: dict) -> str:
    """查看指定合约当前指标面板；合约须在 watchlist 内（watchlist 未接线时不限制）。

    参数：
        deps: ReviewToolDeps，工具或服务依赖集合
        args: dict，工具调用参数

    返回：
        str，查看指定合约当前指标面板；合约须在 watchlist 内（watchlist 未接线时不限制）
    """
    if deps.indicator_service is None:
        return _SERVICE_MISSING
    contract = _need_str(args, "contract")
    if deps.watchlist and contract not in deps.watchlist:
        return f"合约 {contract} 不在 watchlist 内（可选：{', '.join(deps.watchlist)}）"
    interval = _opt_interval(args)
    return _render_panel(deps.indicator_service.full_panel(contract, interval))


async def get_indicator_config(deps: ReviewToolDeps, args: dict) -> str:
    """当前生效短名单 + 可选指标全集菜单（key=label/分组），供改写前核对。

    参数：
        deps: ReviewToolDeps，工具或服务依赖集合
        args: dict，工具调用参数

    返回：
        str，当前生效短名单 + 可选指标全集菜单（key=label/分组），供改写前核对
    """
    if deps.indicator_config_store is None:
        return _STORE_MISSING
    current = deps.indicator_config_store.load_current()
    lines = [
        f"当前指标短名单（{len(current.shortlist)} 个）：{', '.join(current.shortlist)}",
        "",
        "可选指标全集（改写时只能从下列键中挑选，去重后 1~8 个）：",
        *(f"- {item}" for item in indicator_menu_items()),
    ]
    return "\n".join(lines)


# ---------- 写工具 ----------


async def submit_indicator_config(deps: ReviewToolDeps, args: dict) -> str:
    """提交指标短名单改写（全文替换）；校验拒绝返回原因文本，成功记 deps 版本号。

    参数：
        deps: ReviewToolDeps，工具或服务依赖集合
        args: dict，工具调用参数

    返回：
        str，提交指标短名单改写（全文替换）；校验拒绝返回原因文本，成功记 deps 版本号
    """
    if deps.indicator_config_store is None:
        return _STORE_MISSING
    shortlist = _need_str_list(args, "shortlist")
    reason = _need_str(args, "reason")
    try:
        # report_id 跟随 submit_strategy_revision 取法：工具执行时报告尚未生成，置 None，
        # 版本↔报告关联由 ReviewAgent 轮末回填（deps.indicator_config_version_id 驱动）
        version = await deps.indicator_config_store.revise(
            shortlist, created_by="review_agent", reason=reason
        )
    except IndicatorConfigValidationError as e:
        return "校验拒绝：" + "；".join(e.reasons) + "（原短名单未改动，修正后可重新提交）"
    deps.indicator_config_version_id = version.id
    deps.indicator_draft_ids.append(version.id)
    # 展示去重保序后的名单（与 _validated 的生效口径一致）
    deduped = list(dict.fromkeys(shortlist))
    return (
        f"校验通过，指标短名单修订已存为草稿 v{version.id}（{len(deduped)} 个："
        f"{', '.join(deduped)}）；本轮复盘报告提交成功后统一生效，报告失败则自动废弃"
    )
