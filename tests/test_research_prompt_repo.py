"""研报提示词版本子仓库的存取契约（issue #113）。

覆盖：save/list/get 基本读写、状态机流转（draft→applied/discarded）、
latest_applied 只认 applied、discard_all_drafts 清理孤儿、attach 反向关联复盘报告。
"""

from __future__ import annotations

import pytest

from src.memory.db import Database
from src.memory.repo import Repo


@pytest.fixture
async def repo(tmp_path):
    """构造指向临时数据库的 Repo 实例，测试结束后关闭数据库连接。

    参数：
        tmp_path: pytest 临时目录夹具，SQLite 数据库文件落在其中

    返回：
        AsyncIterator[Repo]，yield 已打开临时数据库的仓储对象，最终关闭数据库连接
    """
    db = Database()
    await db.open(tmp_path / "research-prompt-repo.db")
    try:
        yield Repo(db)
    finally:
        await db.close()


async def test_save_and_read_roundtrip(repo: Repo) -> None:
    """落库版本可按 id 读回且字段齐全；列表按 id 倒序（最新在前）。

    参数：
        repo: Repo，临时数据库仓储

    返回：
        None，断言读回内容与写入一致
    """
    v1 = await repo.research_prompt.save_version("正文一", "md5-1", "human", "初始版本")
    v2 = await repo.research_prompt.save_version(
        "正文二", "md5-2", "review_agent", "复盘修订", status="draft"
    )
    got = await repo.research_prompt.get_version(v2.id)
    assert got is not None
    assert got.content == "正文二"
    assert got.status == "draft"
    assert got.created_by == "review_agent"
    assert got.review_report_id is None
    listed = await repo.research_prompt.list_versions()
    assert [v.id for v in listed] == [v2.id, v1.id]
    assert await repo.research_prompt.get_version(9999) is None


async def test_status_flow_and_latest_applied(repo: Repo) -> None:
    """latest_applied_version 只取 applied；draft 生效后成为最新 applied。

    参数：
        repo: Repo，临时数据库仓储

    返回：
        None，断言状态流转后的查询口径
    """
    v1 = await repo.research_prompt.save_version("正文一", "md5-1", "human", "初始版本")
    v2 = await repo.research_prompt.save_version(
        "正文二", "md5-2", "review_agent", "复盘修订", status="draft"
    )
    latest = await repo.research_prompt.latest_applied_version()
    assert latest is not None and latest.id == v1.id  # draft 不算生效版本
    await repo.research_prompt.set_version_status(v2.id, "applied")
    latest = await repo.research_prompt.latest_applied_version()
    assert latest is not None and latest.id == v2.id


async def test_discard_all_drafts(repo: Repo) -> None:
    """孤儿草稿清理只动 draft：applied 版本不受影响，返回废弃条数。

    参数：
        repo: Repo，临时数据库仓储

    返回：
        None，断言草稿被废弃、已生效版本保留
    """
    await repo.research_prompt.save_version("正文一", "md5-1", "human", "初始版本")
    await repo.research_prompt.save_version(
        "正文二", "md5-2", "review_agent", "草稿", status="draft"
    )
    await repo.research_prompt.save_version(
        "正文三", "md5-3", "review_agent", "草稿", status="draft"
    )
    assert await repo.research_prompt.discard_all_drafts() == 2
    statuses = {v.status for v in await repo.research_prompt.list_versions()}
    assert statuses == {"applied", "discarded"}
    assert await repo.research_prompt.discard_all_drafts() == 0  # 幂等：无草稿可清


async def test_attach_report_to_version(repo: Repo) -> None:
    """attach 回填复盘报告 id（版本先落库、报告后落库的反向关联）。

    参数：
        repo: Repo，临时数据库仓储

    返回：
        None，断言 review_report_id 被正确回填
    """
    v = await repo.research_prompt.save_version("正文", "md5-1", "review_agent", "复盘修订")
    assert v.review_report_id is None
    await repo.research_prompt.attach_report_to_version(v.id, 42)
    got = await repo.research_prompt.get_version(v.id)
    assert got is not None and got.review_report_id == 42


async def test_get_version_by_md5(repo: Repo) -> None:
    """按 md5 反解版本：命中同 md5 最新一条，未命中返回 None（issue #113 R6）。

    参数：
        repo: Repo，临时数据库仓储

    返回：
        None，断言命中最新版本与未命中降级
    """
    await repo.research_prompt.save_version("正文旧", "md5-x", "human", "初始版本")
    v2 = await repo.research_prompt.save_version("正文新", "md5-x", "review_agent", "复盘修订")
    got = await repo.research_prompt.get_version_by_md5("md5-x")
    assert got is not None and got.id == v2.id and got.content == "正文新"
    assert await repo.research_prompt.get_version_by_md5("md5-不存在") is None
