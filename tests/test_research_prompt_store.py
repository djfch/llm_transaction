"""研报提示词版本存储（ResearchPromptStore）生命周期测试（issue #113）。

覆盖：启动播种、草稿修订→生效、即时修订、写前校验拒绝、失败废弃、
启动对账（孤儿草稿清理 + 文件与库不一致恢复）、回滚记新版本。
"""

from __future__ import annotations

import asyncio

import pytest

from src.memory.db import Database
from src.memory.repo import Repo
from src.research.prompt_store import ResearchPromptStore, ResearchPromptValidationError

_INIT = "初始研报提示词：" + "先事实后判断，逐标的给结论。" * 10


@pytest.fixture
async def env(tmp_path):
    """构造研报提示词存储测试环境：临时数据库 + 临时提示词文件。

    参数：
        tmp_path: pytest 临时目录夹具

    返回：
        AsyncIterator[SimpleNamespace]，含 store/repo/path 的测试环境，结束后关闭数据库
    """
    from types import SimpleNamespace

    db = Database()
    await db.open(tmp_path / "research-prompt-store.db")
    repo = Repo(db)
    path = tmp_path / "research_prompt.md"
    path.write_text(_INIT, encoding="utf-8")
    events: list[str] = []
    store = ResearchPromptStore(path, repo, on_change=lambda: events.append("changed"))
    try:
        yield SimpleNamespace(store=store, repo=repo, path=path, events=events)
    finally:
        await db.close()


async def test_seed_if_empty(env) -> None:
    """版本表为空且文件存在时播种 v1（human/初始版本）；重复播种不再新增。

    参数：
        env: SimpleNamespace，测试环境

    返回：
        None，断言播种只发生一次且内容来自文件
    """
    v1 = await env.store.seed_if_empty()
    assert v1 is not None and v1.created_by == "human" and v1.content == _INIT
    assert await env.store.seed_if_empty() is None
    assert len(await env.repo.research_prompt.list_versions()) == 1


async def test_seed_skipped_when_file_missing(env) -> None:
    """提示词文件不存在且无缓存正文时不播种（版本表保持为空）。

    参数：
        env: SimpleNamespace，测试环境

    返回：
        None，断言删文件后播种返回 None
    """
    env.path.unlink()
    assert await env.store.seed_if_empty() is None
    assert await env.repo.research_prompt.list_versions() == []


async def test_revise_draft_then_apply(env) -> None:
    """revise 只落 draft 不动文件；apply_version 原子写文件并置 applied、触发变更回调。

    参数：
        env: SimpleNamespace，测试环境

    返回：
        None，断言草稿期间文件不变、生效后文件与状态一致
    """
    await env.store.seed_if_empty()
    new_content = "修订后提示词：" + "逐条依据评价，先看反对证据。" * 10
    draft = await env.store.revise(new_content, "复盘修订", created_by="review_agent")
    assert draft.status == "draft"
    assert env.store.current() == _INIT  # 草稿期文件不动
    applied = await env.store.apply_version(draft.id)
    assert applied.status == "applied"
    assert env.store.current() == new_content
    assert env.events == ["changed"]


async def test_revise_validation_rejects(env) -> None:
    """过短与无差异内容均被拒：不落版本、文件不动。

    参数：
        env: SimpleNamespace，测试环境

    返回：
        None，断言两种拒绝各携带对应原因
    """
    await env.store.seed_if_empty()
    with pytest.raises(ResearchPromptValidationError) as short_err:
        await env.store.revise("太短", "x", created_by="review_agent")
    assert any("过短" in r for r in short_err.value.reasons)
    with pytest.raises(ResearchPromptValidationError) as same_err:
        await env.store.revise(_INIT, "x", created_by="review_agent")
    assert same_err.value.no_diff_only is True
    assert len(await env.repo.research_prompt.list_versions()) == 1  # 只有种子版本


async def test_revise_applied_immediate(env) -> None:
    """人工即时修订：落库即生效（文件已更新、版本 applied、默认 human 来源）。

    参数：
        env: SimpleNamespace，测试环境

    返回：
        None，断言 revise_applied 一步完成写文件与生效
    """
    await env.store.seed_if_empty()
    new_content = "人工修订：" + "提高证据门槛。" * 20
    version = await env.store.revise_applied(new_content, "人工调优")
    assert version.status == "applied" and version.created_by == "human"
    assert env.store.current() == new_content


async def test_discard_draft(env) -> None:
    """报告失败路径：草稿置 discarded，文件保持旧内容。

    参数：
        env: SimpleNamespace，测试环境

    返回：
        None，断言草稿状态与文件内容
    """
    await env.store.seed_if_empty()
    draft = await env.store.revise(
        "修订：" + "不会生效。" * 30, "注定失败", created_by="review_agent"
    )
    await env.store.discard_draft(draft.id)
    got = await env.repo.research_prompt.get_version(draft.id)
    assert got is not None and got.status == "discarded"
    assert env.store.current() == _INIT


async def test_reconcile_restores_file_and_discards_orphans(env) -> None:
    """启动对账：孤儿草稿废弃；文件被外部改动后以最新 applied 版本为准恢复并通知。

    参数：
        env: SimpleNamespace，测试环境

    返回：
        None，断言草稿被清、文件被恢复、回调被触发
    """
    await env.store.seed_if_empty()
    await env.store.revise("孤儿草稿：" + "残留内容。" * 30, "上轮遗留", created_by="review_agent")
    env.path.write_text("被外部改坏的文件", encoding="utf-8")
    await env.store.reconcile()
    drafts = [v for v in await env.repo.research_prompt.list_versions() if v.status == "draft"]
    assert drafts == []
    assert env.store.current() == _INIT
    assert env.events == ["changed"]


async def test_reconcile_noop_when_consistent(env) -> None:
    """文件与最新 applied 一致时对账静默通过：不触发回调、不改文件。

    参数：
        env: SimpleNamespace，测试环境

    返回：
        None，断言无通知且文件不变
    """
    await env.store.seed_if_empty()
    await env.store.reconcile()
    assert env.events == []
    assert env.store.current() == _INIT


async def test_rollback_writes_content_and_records_new_version(env) -> None:
    """回滚到历史版本：写回其内容并记 created_by='rollback' 的新版本；不存在版本报错。

    参数：
        env: SimpleNamespace，测试环境

    返回：
        None，断言回滚后文件内容、版本来源与异常分支
    """
    await env.store.seed_if_empty()
    v2 = await env.store.revise_applied("第二版：" + "加一段宏观纪律。" * 20, "人工修订")
    rolled = await env.store.rollback(1)
    assert rolled.created_by == "rollback" and rolled.status == "applied"
    assert env.store.current() == _INIT
    assert rolled.id > v2.id
    with pytest.raises(ResearchPromptValidationError):
        await env.store.rollback(9999)


async def test_rollback_rejects_non_applied_version(env) -> None:
    """回滚目标为草稿/已废弃版本时拒绝（审查 P2-4）：文件不动、不落新版本。

    参数：
        env: SimpleNamespace，测试环境

    返回：
        None，断言两种非 applied 状态均抛 ResearchPromptValidationError 且无副作用
    """
    await env.store.seed_if_empty()
    draft = await env.store.revise("草稿版：" + "未生效内容。" * 20, "复盘草稿", "review_agent")
    with pytest.raises(ResearchPromptValidationError, match="只能回滚到已生效版本"):
        await env.store.rollback(draft.id)
    await env.store.discard_draft(draft.id)
    with pytest.raises(ResearchPromptValidationError, match="只能回滚到已生效版本"):
        await env.store.rollback(draft.id)
    assert env.store.current() == _INIT
    assert len(await env.repo.research_prompt.list_versions()) == 2  # v1 + 已废弃草稿，无新增
    assert env.events == []


# ---------- 生效锁取代检测（issue #113 F11） ----------


async def test_apply_version_yields_to_newer_applied(env) -> None:
    """旧草稿生效时若已存在更高 id 的 applied 版本，则被取代置 discarded 且不覆盖文件。

    参数：
        env: SimpleNamespace，测试环境

    返回：
        None，断言 apply 返回 None、文件保留人工内容、旧草稿状态为 discarded
    """
    await env.store.seed_if_empty()
    draft = await env.store.revise(
        "草稿版提示词：" + "先事实后判断，逐标的给结论。" * 10, "复盘修订", "review_agent"
    )
    human = await env.store.revise_applied("人工提示词：" + "提高证据门槛。" * 20, "人工调优")
    applied = await env.store.apply_version(draft.id)
    assert applied is None  # 已被更高 applied 版本取代
    assert env.store.current() == human.content  # 文件保留人工内容
    assert (await env.store.get_version(draft.id)).status == "discarded"
    assert (await env.store.get_version(human.id)).status == "applied"


async def test_rollback_and_apply_interleave_keeps_file_consistent(env, monkeypatch) -> None:
    """rollback 与 apply_version 并发时全程互斥：文件始终等于库内最新 applied 版本内容。

    在 rollback 记新版本前插入延时制造确定性交错：无锁时 apply_version 会插队先生效
    草稿，rollback 随后落库更高 id 的回滚版本而文件停在草稿内容；rollback 收进
    生效锁后两者串行，文件与库内最新 applied 版本必然一致（issue #113 R7）。

    参数：
        env: SimpleNamespace，测试环境
        monkeypatch: MonkeyPatch，用于给记版本方法插入延时

    返回：
        None，断言文件内容与库内最新 applied 版本内容一致
    """
    v1 = await env.store.seed_if_empty()
    assert v1 is not None
    draft = await env.store.revise(
        "草稿版提示词：" + "先事实后判断，逐标的给结论。" * 10, "复盘修订", "review_agent"
    )
    original_save = env.repo.research_prompt.save_version

    async def slow_save(*args, **kwargs):
        """记版本前延时 50ms，制造 rollback 写文件后、记版本前的插队窗口。

        参数：
            *args: 原 save_version 的位置参数
            **kwargs: 原 save_version 的关键字参数

        返回：
            原 save_version 的返回（透传）
        """
        await asyncio.sleep(0.05)
        return await original_save(*args, **kwargs)

    monkeypatch.setattr(env.repo.research_prompt, "save_version", slow_save)
    await asyncio.gather(env.store.rollback(v1.id), env.store.apply_version(draft.id))
    latest = await env.repo.research_prompt.latest_applied_version()
    assert latest is not None
    assert env.store.current() == latest.content
