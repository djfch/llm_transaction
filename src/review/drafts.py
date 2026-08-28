"""复盘草稿通道：策略书/指标短名单/研报提示词三类草稿的统一生效与废弃（issue #62/#73/#113）。

从 review/agent.py 拆出（文件行数门禁）：三类 store 的草稿语义完全一致——
revise 只落 draft 不动文件，报告成功后经 apply_version 统一生效，失败/取消置
discarded；"草稿已被人工更高版本取代"的判定收口在各 store 的生效锁内
（apply_version 返回 None 即被取代，store 侧已置 discarded，issue #113 F11），
本模块不再做生效前快照比对——快照与生效之间的等待窗口正是竞态来源。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from src.audit.logger import get_logger

if TYPE_CHECKING:
    from src.review.tool_handlers import ReviewToolDeps

logger = get_logger(__name__)

# 通道键 → 运行时文件名：生效失败的异常/告警/事件按通道指明目标文件（issue #113 R9）
CHANNEL_FILES = {
    "strategy": "system_prompt.md",
    "indicator_config": "indicator_config.yaml",
    "research_prompt": "research_prompt.md",
}


def apply_failed_files(failed: list[tuple[str, int]]) -> list[str]:
    """把生效失败列表转成涉及的去重文件名列表（保持通道顺序）。

    参数：
        failed: list[tuple[str, int]]，（通道键, 草稿版本 id）失败列表

    返回：
        list[str]：涉及的运行时文件名；未知通道键降级为通道键本身
    """
    files: list[str] = []
    for channel, _draft_id in failed:
        name = CHANNEL_FILES.get(channel, channel)
        if name not in files:
            files.append(name)
    return files


def format_apply_failures(failed: list[tuple[str, int]]) -> str:
    """把生效失败列表格式化为「文件名 草稿 vN」顿号串（异常与告警文案复用）。

    参数：
        failed: list[tuple[str, int]]，（通道键, 草稿版本 id）失败列表

    返回：
        str：如「system_prompt.md 草稿 v3、indicator_config.yaml 草稿 v5」
    """
    return "、".join(
        f"{CHANNEL_FILES.get(channel, channel)} 草稿 v{draft_id}" for channel, draft_id in failed
    )


def _channels(deps: ReviewToolDeps) -> list[tuple[Any, list[int], str, str]]:
    """组装本轮生效通道列表：（store、草稿 id 列表、通道键、通道中文名）。

    未装配的可选通道（indicator_config_store / research_prompt_store 为 None）跳过。

    参数：
        deps: ReviewToolDeps，本轮工具依赖（读取 store 与草稿 id 列表）

    返回：
        list[tuple[Any, list[int], str, str]]：待处理通道列表
    """
    channels: list[tuple[Any, list[int], str, str]] = [
        (deps.store, deps.strategy_draft_ids, "strategy", "策略"),
    ]
    if deps.indicator_config_store is not None:
        channels.append(
            (deps.indicator_config_store, deps.indicator_draft_ids, "indicator_config", "指标配置")
        )
    if deps.research_prompt_store is not None:
        channels.append(
            (
                deps.research_prompt_store,
                deps.research_prompt_draft_ids,
                "research_prompt",
                "研报提示词",
            )
        )
    return channels


async def apply_drafts(deps: ReviewToolDeps) -> None:
    """统一生效本轮草稿：失败收集 + 被取代草稿跳过（issue #100/#113 F11）。

    取代判定在 store 生效锁内完成（apply_version 返回 None）：store 已把被取代
    草稿置 discarded 并告警，此处只跳过、不计入失败；单个 apply 抛异常不中断
    其余草稿，失败按 (通道键, 草稿 id) 记入 deps.apply_failed_ids 供事件与告警
    按通道指明文件（issue #113 R9）。每次生效尝试开头先清空失败集合重算：
    _complete_interrupted 的补全收尾会对同一 deps 重试生效，残留的旧失败记录
    会让告警/事件把已生效的草稿误报为失败、并重复累计（V4）。

    参数：
        deps: ReviewToolDeps，本轮工具依赖

    返回：
        None，生效/废弃就地完成；失败 (通道键, id) 就地重算记入
        deps.apply_failed_ids
    """
    deps.apply_failed_ids.clear()  # 每次生效尝试重算失败集合，杜绝重试残留
    for store, draft_ids, channel, label in _channels(deps):
        for draft_id in draft_ids:
            try:
                applied = await store.apply_version(draft_id)
            except Exception:
                deps.apply_failed_ids.append((channel, draft_id))
                logger.exception("%s草稿生效失败（draft_id=%s）", label, draft_id)
                continue
            if applied is None:
                logger.warning("%s草稿 v%d 已被更高版本取代，跳过生效", label, draft_id)


async def discard_drafts(deps: ReviewToolDeps) -> None:
    """报告失败/取消时废弃本轮全部草稿版本；文件从未被动过，无需回滚（issue #73）。

    参数：
        deps: ReviewToolDeps，本轮工具依赖（读取其落库的草稿 id 列表）

    返回：
        None，逐个置 discarded；单个失败只记日志不中断其余废弃
    """
    channels: list[tuple[Any, list[int], str]] = [(deps.store, deps.strategy_draft_ids, "策略")]
    if deps.indicator_config_store is not None:
        channels.append((deps.indicator_config_store, deps.indicator_draft_ids, "指标"))
    if deps.research_prompt_store is not None:
        channels.append((deps.research_prompt_store, deps.research_prompt_draft_ids, "研报提示词"))
    for store, draft_ids, label in channels:
        for draft_id in draft_ids:
            try:
                await store.discard_draft(draft_id)
            except Exception:
                logger.exception("%s草稿废弃失败 draft_id=%s", label, draft_id)
