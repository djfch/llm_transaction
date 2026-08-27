"""复盘侧的研报提示词版本工具（issue #113）：1 只读 + 1 写。

- get_research_prompt_versions：版本历史/指定版本全文 + 当前正文（修订前核对用）；
- submit_research_prompt_revision：研报提示词写出口，草稿模式与策略书同口径——
  校验通过只落 draft 版本、文件不动，复盘报告成功才统一生效，失败/取消自动废弃。
deps.research_prompt_store 未装配（None）时两工具返回中文降级提示，不中断本轮。
"""

from __future__ import annotations

from src.research.prompt_store import ResearchPromptValidationError
from src.review.tool_handlers import ReviewToolDeps, _fmt_time, _need_str, _to_int

_VERSION_LIST_LIMIT = 50  # 版本列表最多返回条数


async def get_research_prompt_versions(deps: ReviewToolDeps, args: dict) -> str:
    """查看研报提示词版本历史或指定版本全文，并附当前正文。

    参数：
        deps: ReviewToolDeps，复盘工具依赖（用其 research_prompt_store 读取版本）
        args: dict，工具参数：version_id（可选；提供时返回该版本详情与全文，
            缺省时返回版本列表与当前正文）

    返回：
        str：版本详情或版本列表文本；未装配时返回降级提示，指定版本不存在时返回提示
    """
    store = deps.research_prompt_store
    if store is None:
        return "研报提示词版本功能未装配（research_prompt_store 为空），无法查询版本"
    vid_arg = args.get("version_id")
    if vid_arg is not None:
        v = await store.get_version(_to_int(vid_arg, "version_id"))
        if v is None:
            return f"研报提示词版本 v{vid_arg} 不存在"
        return (
            f"v{v.id} | 状态={v.status} | 来源={v.created_by} | md5={v.md5}"
            f" | 时间={_fmt_time(v.created_at)} | 理由={v.reason}\n全文：\n{v.content}"
        )
    versions = (await store.list_versions())[:_VERSION_LIST_LIMIT]
    lines = [f"研报提示词版本共 {len(versions)} 个（最新在前）："]
    for v in versions:
        lines.append(
            f"- v{v.id} | 状态={v.status} | 来源={v.created_by} | md5={v.md5[:8]}"
            f" | 时间={_fmt_time(v.created_at)} | 理由={v.reason}"
        )
    lines += ["", "当前研报提示词全文：", store.current() or "（研报提示词文件不存在）"]
    return "\n".join(lines)


async def submit_research_prompt_revision(deps: ReviewToolDeps, args: dict) -> str:
    """提交修订后的研报提示词；校验通过则落草稿版本，本轮复盘报告成功后统一生效。

    参数：
        deps: ReviewToolDeps，复盘工具依赖（成功时回写 research_prompt_version_id 与草稿列表）
        args: dict，工具参数：new_prompt_md（必填，新提示词全文）、reason（必填，修订理由）

    返回：
        str：提交结果文本；校验拒绝时列出原因且原提示词不变；未装配时返回降级提示
    """
    store = deps.research_prompt_store
    if store is None:
        return "研报提示词版本功能未装配（research_prompt_store 为空），修订未提交"
    new_prompt_md = _need_str(args, "new_prompt_md")
    reason = _need_str(args, "reason")
    try:
        version = await store.revise(new_prompt_md, reason, created_by="review_agent")
    except ResearchPromptValidationError as e:
        return "校验拒绝：" + "；".join(e.reasons) + "（原研报提示词未改动，修正后可重新提交）"
    deps.research_prompt_version_id = version.id
    deps.research_prompt_draft_ids.append(version.id)
    return (
        f"校验通过，研报提示词修订已存为草稿 v{version.id}（md5={version.md5[:8]}）；"
        "本轮复盘报告提交成功后统一生效，报告失败则自动废弃"
    )
