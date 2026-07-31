"""src/review/strategy.py 策略版本管理测试：tmp_path 策略书文件 + tmp_path 真实 SQLite。

覆盖：播种 v1（幂等/缺文件跳过）、revise 成功（文件/md5/版本行）、三类校验拒绝
（短/超长/无差异：原文件不动、无新版本、原因列表）、rollback（内容回写 + 新版本
created_by='rollback'）、版本不存在报错。
"""

import hashlib

import pytest

from src.agent.prompts import PromptLoader
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
    assert len(await repo.review.list_strategy_versions()) == 1


async def test_seed_skips_when_prompt_missing(tmp_path, repo):
    store = StrategyStore(tmp_path / "nope.md", repo)
    assert await store.seed_if_empty() is None
    assert await repo.review.list_strategy_versions() == []


# ---------- revise ----------


async def test_revise_success(store, repo, prompt_path):
    v = await store.revise(_NEW, "复盘改进", created_by="review_agent")
    assert prompt_path.read_text(encoding="utf-8") == _NEW  # 文件已原子替换
    assert v.md5 == content_md5(_NEW)
    assert v.created_by == "review_agent" and v.reason == "复盘改进"
    assert store.current() == _NEW
    v2 = await store.revise(_NEW + "补充条款。" * 20, "再改", "human", report_id=7)
    assert v2.report_id == 7  # report_id 透传落库
    assert [x.id for x in await repo.review.list_strategy_versions()] == [v2.id, v.id]  # 倒序


async def test_revise_rejects_too_short(store, repo, prompt_path):
    with pytest.raises(StrategyValidationError) as exc_info:
        await store.revise("太短了", "r", "review_agent")
    assert any("100" in r for r in exc_info.value.reasons)
    assert prompt_path.read_text(encoding="utf-8") == _INIT  # 原文件不动
    assert await repo.review.list_strategy_versions() == []  # 无新版本


async def test_revise_rejects_too_long(store, repo, prompt_path):
    with pytest.raises(StrategyValidationError) as exc_info:
        await store.revise("长" * 12000, "r", "review_agent")  # 36000 字节 > 32KB
    assert any("32KB" in r for r in exc_info.value.reasons)
    assert prompt_path.read_text(encoding="utf-8") == _INIT
    assert await repo.review.list_strategy_versions() == []


async def test_revise_rejects_no_diff(store, repo, prompt_path):
    with pytest.raises(StrategyValidationError) as exc_info:
        await store.revise(_INIT, "r", "review_agent")
    assert any("无差异" in r for r in exc_info.value.reasons)
    assert prompt_path.read_text(encoding="utf-8") == _INIT
    assert await repo.review.list_strategy_versions() == []


async def test_revise_collects_all_reasons(tmp_path, repo):
    """一次违反多条时 reasons 携带全部原因（LLM 可逐项修正）。"""
    path = tmp_path / "system_prompt.md"
    path.write_text("短内容", encoding="utf-8")  # <100 字符
    store = StrategyStore(path, repo)
    with pytest.raises(StrategyValidationError) as exc_info:
        await store.revise("短内容", "r", "review_agent")  # 既过短又与当前无差异
    assert len(exc_info.value.reasons) == 2


# ---------- 换行归一化（Windows CRLF） ----------


async def test_revise_normalizes_crlf_content(store, repo, prompt_path):
    """CRLF 归一化回归：CRLF 提交内容落盘为纯 LF（文件字节无 \r），且版本行
    md5 == hashlib.md5(归一化内容) == PromptLoader.body_md5()（版本↔决策 join 不断裂）。"""
    content = "新策略书：\r\n" + "顺势加仓，严格止损。\r\n" * 10
    v = await store.revise(content, "Windows 编辑器提交", created_by="human")
    assert b"\r" not in prompt_path.read_bytes()  # 文件字节无 \r
    normalized = content.replace("\r\n", "\n")
    assert v.md5 == hashlib.md5(normalized.encode("utf-8")).hexdigest()
    assert PromptLoader(prompt_path).body_md5() == v.md5


async def test_rollback_normalizes_historical_crlf(store, repo, prompt_path):
    """历史脏行（content 含 \r\n）回滚时同样归一化：落盘纯 LF，新版本 md5 按归一化内容重算。"""
    dirty = "脏策略书：\r\n" + "历史遗留内容。\r\n" * 10
    v1 = await repo.review.save_strategy_version(dirty, "md5-dirty", "human", "历史版本")
    v3 = await store.rollback(v1.id)
    assert b"\r" not in prompt_path.read_bytes()
    assert v3.md5 == content_md5(dirty.replace("\r\n", "\n"))
    assert PromptLoader(prompt_path).body_md5() == v3.md5


# ---------- rollback ----------


async def test_rollback_writes_back_and_records(store, repo, prompt_path):
    v1 = await store.seed_if_empty()
    v2 = await store.revise(_NEW, "改进", "review_agent")
    v3 = await store.rollback(v1.id)
    assert prompt_path.read_text(encoding="utf-8") == v1.content  # 内容回写
    assert v3.created_by == "rollback" and v3.reason == f"回滚到 v{v1.id}"
    assert v3.md5 == v1.md5  # 回写内容与原版本同 md5
    versions = await repo.review.list_strategy_versions()
    assert [v.id for v in versions] == [v3.id, v2.id, v1.id]  # 最新在前


async def test_rollback_missing_version(store):
    with pytest.raises(StrategyValidationError, match="不存在"):
        await store.rollback(999)


# ---------- 结构化判定字段 no_diff_only ----------


async def test_validation_error_no_diff_only_flag(store, repo, prompt_path):
    """no_diff_only：唯一原因是"与当前版本无差异"时为 True；其余校验失败为 False。"""
    with pytest.raises(StrategyValidationError) as exc_info:
        await store.revise(_INIT, "r", "human")  # 唯一原因是无差异
    assert exc_info.value.no_diff_only is True
    with pytest.raises(StrategyValidationError) as exc_info2:
        await store.revise("太短了", "r", "human")  # 唯一原因是过短
    assert exc_info2.value.no_diff_only is False
    with pytest.raises(StrategyValidationError) as exc_info3:
        await store.rollback(999)  # 版本不存在
    assert exc_info3.value.no_diff_only is False


async def test_validation_error_no_diff_only_false_when_mixed(tmp_path, repo):
    """无差异 + 过短并存时 no_diff_only=False（非唯一原因，人工重复保存不视为幂等）。"""
    path = tmp_path / "system_prompt.md"
    path.write_text("短内容", encoding="utf-8")
    store = StrategyStore(path, repo)
    with pytest.raises(StrategyValidationError) as exc_info:
        await store.revise("短内容", "r", "review_agent")
    assert len(exc_info.value.reasons) == 2
    assert exc_info.value.no_diff_only is False


# ---------- 变更回调 on_change（WS strategy_updated 接线点） ----------


async def test_on_change_fired_on_revise_and_rollback(prompt_path, repo):
    """revise/rollback 落版本后各触发一次 on_change；校验失败不触发。"""
    calls: list[int] = []
    store = StrategyStore(prompt_path, repo, on_change=lambda: calls.append(1))
    v1 = await store.seed_if_empty()
    assert calls == []  # 播种不算变更（启动时无前端需要通知）
    await store.revise(_NEW, "改进", "review_agent")
    assert len(calls) == 1
    await store.rollback(v1.id)
    assert len(calls) == 2
    with pytest.raises(StrategyValidationError):
        await store.revise("太短了", "r", "review_agent")  # 校验拒绝：不触发
    with pytest.raises(StrategyValidationError):
        await store.rollback(999)  # 版本不存在：不触发
    assert len(calls) == 2


async def test_on_change_absent_is_noop(store, repo):
    """未接线 on_change（默认 None）时 revise 照常工作不报错。"""
    v = await store.revise(_NEW, "改进", "review_agent")
    assert v.md5 == content_md5(_NEW)
