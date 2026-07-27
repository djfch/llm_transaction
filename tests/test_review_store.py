"""src/review/strategy.py 策略版本管理测试：tmp_path 策略书文件 + tmp_path 真实 SQLite。

覆盖：播种 v1（幂等/缺文件跳过）、revise 成功（文件/md5/版本行）、三类校验拒绝
（短/超长/无差异：原文件不动、无新版本、原因列表）、rollback（内容回写 + 新版本
created_by='rollback'）、版本不存在报错。
"""

import hashlib

import pytest

from src.memory import Database, Repo
from src.review.strategy import StrategyStore, StrategyValidationError, content_md5

_INIT = "初始策略书：" + "稳健交易，控制回撤。" * 10  # ≥100 字符
_NEW = "新策略书：" + "顺势加仓，严格止损。" * 10


@pytest.fixture
async def repo(tmp_path):
    db = Database()
    await db.open(tmp_path / "test.db")
    yield Repo(db)
    await db.close()


@pytest.fixture
def prompt_path(tmp_path):
    path = tmp_path / "system_prompt.md"
    path.write_text(_INIT, encoding="utf-8")
    return path


@pytest.fixture
async def store(prompt_path, repo):
    return StrategyStore(prompt_path, repo)


# ---------- 播种 ----------


async def test_seed_if_empty_creates_v1(store, repo, prompt_path):
    v = await store.seed_if_empty()
    assert v is not None
    assert v.id == 1 and v.created_by == "human" and v.reason == "初始版本"
    assert v.content == _INIT
    assert v.md5 == hashlib.md5(_INIT.encode("utf-8")).hexdigest()  # 与 prompts.py 同算法
    # 幂等：版本表非空后不再播种
    assert await store.seed_if_empty() is None
    assert len(await repo.list_strategy_versions()) == 1


async def test_seed_skips_when_prompt_missing(tmp_path, repo):
    store = StrategyStore(tmp_path / "nope.md", repo)
    assert await store.seed_if_empty() is None
    assert await repo.list_strategy_versions() == []


# ---------- revise ----------


async def test_revise_success(store, repo, prompt_path):
    v = await store.revise(_NEW, "复盘改进", created_by="review_agent")
    assert prompt_path.read_text(encoding="utf-8") == _NEW  # 文件已原子替换
    assert v.md5 == content_md5(_NEW)
    assert v.created_by == "review_agent" and v.reason == "复盘改进"
    assert store.current() == _NEW
    v2 = await store.revise(_NEW + "补充条款。" * 20, "再改", "human", report_id=7)
    assert v2.report_id == 7  # report_id 透传落库
    assert [x.id for x in await repo.list_strategy_versions()] == [v2.id, v.id]  # 倒序


async def test_revise_rejects_too_short(store, repo, prompt_path):
    with pytest.raises(StrategyValidationError) as exc_info:
        await store.revise("太短了", "r", "review_agent")
    assert any("100" in r for r in exc_info.value.reasons)
    assert prompt_path.read_text(encoding="utf-8") == _INIT  # 原文件不动
    assert await repo.list_strategy_versions() == []  # 无新版本


async def test_revise_rejects_too_long(store, repo, prompt_path):
    with pytest.raises(StrategyValidationError) as exc_info:
        await store.revise("长" * 12000, "r", "review_agent")  # 36000 字节 > 32KB
    assert any("32KB" in r for r in exc_info.value.reasons)
    assert prompt_path.read_text(encoding="utf-8") == _INIT
    assert await repo.list_strategy_versions() == []


async def test_revise_rejects_no_diff(store, repo, prompt_path):
    with pytest.raises(StrategyValidationError) as exc_info:
        await store.revise(_INIT, "r", "review_agent")
    assert any("无差异" in r for r in exc_info.value.reasons)
    assert prompt_path.read_text(encoding="utf-8") == _INIT
    assert await repo.list_strategy_versions() == []


async def test_revise_collects_all_reasons(tmp_path, repo):
    """一次违反多条时 reasons 携带全部原因（LLM 可逐项修正）。"""
    path = tmp_path / "system_prompt.md"
    path.write_text("短内容", encoding="utf-8")  # <100 字符
    store = StrategyStore(path, repo)
    with pytest.raises(StrategyValidationError) as exc_info:
        await store.revise("短内容", "r", "review_agent")  # 既过短又与当前无差异
    assert len(exc_info.value.reasons) == 2


# ---------- rollback ----------


async def test_rollback_writes_back_and_records(store, repo, prompt_path):
    v1 = await store.seed_if_empty()
    v2 = await store.revise(_NEW, "改进", "review_agent")
    v3 = await store.rollback(v1.id)
    assert prompt_path.read_text(encoding="utf-8") == v1.content  # 内容回写
    assert v3.created_by == "rollback" and v3.reason == f"回滚到 v{v1.id}"
    assert v3.md5 == v1.md5  # 回写内容与原版本同 md5
    versions = await repo.list_strategy_versions()
    assert [v.id for v in versions] == [v3.id, v2.id, v1.id]  # 最新在前


async def test_rollback_missing_version(store):
    with pytest.raises(StrategyValidationError, match="不存在"):
        await store.rollback(999)
