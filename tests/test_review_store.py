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
    """构造指向临时 SQLite 数据库的 Repo 仓储，测试结束后关闭连接。

    参数：
        tmp_path: Path，pytest 临时目录夹具，test.db 数据库文件落在其中

    返回：
        AsyncIterator[Repo]，yield 已打开临时数据库的仓储对象，用例结束后关闭连接
    """
    db = Database()
    await db.open(tmp_path / "test.db")
    yield Repo(db)
    await db.close()


@pytest.fixture
def prompt_path(tmp_path):
    """在临时目录写入初始策略书文件并返回其路径。

    参数：
        tmp_path: Path，pytest 临时目录夹具，system_prompt.md 写在其中

    返回：
        Path：内容为 _INIT 初始策略书的 system_prompt.md 文件路径
    """
    path = tmp_path / "system_prompt.md"
    path.write_text(_INIT, encoding="utf-8")
    return path


@pytest.fixture
async def store(prompt_path, repo):
    """构造绑定临时策略文件与临时数据库的 StrategyStore。

    参数：
        prompt_path: Path，prompt_path 夹具返回的初始策略书文件路径
        repo: Repo，repo 夹具提供的临时数据库仓储

    返回：
        StrategyStore：策略版本管理对象，文件与数据库均隔离在临时目录
    """
    return StrategyStore(prompt_path, repo)


# ---------- 播种 ----------


async def test_seed_if_empty_creates_v1(store, repo, prompt_path):
    """版本表为空时播种生成 v1 初始版本，重复调用幂等不再插入。

    参数：
        store: StrategyStore，store 夹具提供的策略版本管理对象
        repo: Repo，repo 夹具提供的临时数据库仓储
        prompt_path: Path，prompt_path 夹具返回的初始策略书文件路径

    返回：
        None，断言 v1 的 id/created_by/reason/内容/md5 符合初始版本约定，
        二次播种返回 None 且版本表仅 1 条记录
    """
    v = await store.seed_if_empty()
    assert v is not None
    assert v.id == 1 and v.created_by == "human" and v.reason == "初始版本"
    assert v.content == _INIT
    assert v.md5 == hashlib.md5(_INIT.encode("utf-8")).hexdigest()  # 与 prompts.py 同算法
    # 幂等：版本表非空后不再播种
    assert await store.seed_if_empty() is None
    assert len(await repo.review.list_strategy_versions()) == 1


async def test_seed_skips_when_prompt_missing(tmp_path, repo):
    """策略书文件不存在时播种被跳过，不创建任何版本。

    参数：
        tmp_path: Path，pytest 临时目录夹具，用于拼出不存在的策略文件路径
        repo: Repo，repo 夹具提供的临时数据库仓储

    返回：
        None，断言 seed_if_empty 返回 None 且版本列表为空
    """
    store = StrategyStore(tmp_path / "nope.md", repo)
    assert await store.seed_if_empty() is None
    assert await repo.review.list_strategy_versions() == []


# ---------- revise ----------


async def test_revise_success(store, repo, prompt_path):
    """revise 成功路径：文件原子替换、版本字段正确、report_id 透传、版本列表倒序。

    参数：
        store: StrategyStore，store 夹具提供的策略版本管理对象
        repo: Repo，repo 夹具提供的临时数据库仓储
        prompt_path: Path，prompt_path 夹具返回的初始策略书文件路径

    返回：
        None，断言策略文件已替换为新内容、版本 md5/created_by/reason 正确、
        current() 返回新内容、report_id 落库且版本列表按最新在前排列
    """
    v = await store.revise(_NEW, "复盘改进", created_by="review_agent")
    # 草稿语义（issue #62/#73）：revise 只落 draft，文件与 current() 不变
    assert prompt_path.read_text(encoding="utf-8") != _NEW
    assert v.status == "draft" and v.md5 == content_md5(_NEW)
    applied = await store.apply_version(v.id)
    assert applied.status == "applied"
    assert prompt_path.read_text(encoding="utf-8") == _NEW  # 生效后文件已替换
    assert store.current() == _NEW
    v2 = await store.revise(_NEW + "补充条款。" * 20, "再改", "human", report_id=7)
    assert v2.report_id == 7 and v2.status == "draft"  # report_id 透传落库
    await store.apply_version(v2.id)
    assert [x.id for x in await repo.review.list_strategy_versions()] == [v2.id, v.id]  # 倒序


async def test_revise_rejects_too_short(store, repo, prompt_path):
    """验证策略正文过短时修订被拒绝且文件与版本表均保持不变。

    参数：
        store: StrategyStore，策略版本管理对象
        repo: Repo，临时数据库仓储
        prompt_path: Path，当前策略书文件路径

    返回：
        None，通过断言验证校验原因、原文件内容和空版本列表
    """
    with pytest.raises(StrategyValidationError) as exc_info:
        await store.revise("太短了", "r", "review_agent")
    assert any("100" in r for r in exc_info.value.reasons)
    assert prompt_path.read_text(encoding="utf-8") == _INIT  # 原文件不动
    assert await repo.review.list_strategy_versions() == []  # 无新版本


async def test_revise_rejects_too_long(store, repo, prompt_path):
    """验证策略正文超过字节上限时修订被拒绝且不产生任何持久化变更。

    参数：
        store: StrategyStore，策略版本管理对象
        repo: Repo，临时数据库仓储
        prompt_path: Path，当前策略书文件路径

    返回：
        None，通过断言验证 32KB 原因、原文件内容和空版本列表
    """
    with pytest.raises(StrategyValidationError) as exc_info:
        await store.revise("长" * 12000, "r", "review_agent")  # 36000 字节 > 32KB
    assert any("32KB" in r for r in exc_info.value.reasons)
    assert prompt_path.read_text(encoding="utf-8") == _INIT
    assert await repo.review.list_strategy_versions() == []


async def test_revise_rejects_no_diff(store, repo, prompt_path):
    """验证提交与当前策略完全相同时被判为无差异且不新增版本。

    参数：
        store: StrategyStore，策略版本管理对象
        repo: Repo，临时数据库仓储
        prompt_path: Path，当前策略书文件路径

    返回：
        None，通过断言验证无差异原因、原文件内容和空版本列表
    """
    with pytest.raises(StrategyValidationError) as exc_info:
        await store.revise(_INIT, "r", "review_agent")
    assert any("无差异" in r for r in exc_info.value.reasons)
    assert prompt_path.read_text(encoding="utf-8") == _INIT
    assert await repo.review.list_strategy_versions() == []


async def test_revise_collects_all_reasons(tmp_path, repo):
    """验证一次修订同时违反多条规则时完整收集全部校验原因。

    参数：
        tmp_path: Path，pytest 临时目录
        repo: Repo，临时数据库仓储

    返回：
        None，通过断言验证过短与无差异两个原因同时返回
    """
    path = tmp_path / "system_prompt.md"
    path.write_text("短内容", encoding="utf-8")  # <100 字符
    store = StrategyStore(path, repo)
    with pytest.raises(StrategyValidationError) as exc_info:
        await store.revise("短内容", "r", "review_agent")  # 既过短又与当前无差异
    assert len(exc_info.value.reasons) == 2


# ---------- 换行归一化（Windows CRLF） ----------


async def test_revise_normalizes_crlf_content(store, repo, prompt_path):
    """验证修订时把 CRLF 归一为 LF 并保持文件、版本与提示词摘要一致。

    参数：
        store: StrategyStore，策略版本管理对象
        repo: Repo，临时数据库仓储，本用例通过 store 间接使用
        prompt_path: Path，当前策略书文件路径

    返回：
        None，通过断言验证无回车字节且三处 md5(内容摘要)一致
    """
    content = "新策略书：\r\n" + "顺势加仓，严格止损。\r\n" * 10
    v = await store.revise(content, "Windows 编辑器提交", created_by="human")
    await store.apply_version(v.id)  # 草稿生效后才写文件（issue #62/#73）
    assert b"\r" not in prompt_path.read_bytes()  # 文件字节无 \r
    normalized = content.replace("\r\n", "\n")
    assert v.md5 == hashlib.md5(normalized.encode("utf-8")).hexdigest()
    assert PromptLoader(prompt_path).body_md5() == v.md5


async def test_rollback_normalizes_historical_crlf(store, repo, prompt_path):
    """验证回滚历史 CRLF 策略时同样归一换行并重算新版本摘要。

    参数：
        store: StrategyStore，策略版本管理对象
        repo: Repo，用于直接写入历史脏版本的仓储
        prompt_path: Path，回滚后写入的策略书文件路径

    返回：
        None，通过断言验证文件无回车且版本摘要与提示词摘要一致
    """
    dirty = "脏策略书：\r\n" + "历史遗留内容。\r\n" * 10
    v1 = await repo.review.save_strategy_version(dirty, "md5-dirty", "human", "历史版本")
    v3 = await store.rollback(v1.id)
    assert b"\r" not in prompt_path.read_bytes()
    assert v3.md5 == content_md5(dirty.replace("\r\n", "\n"))
    assert PromptLoader(prompt_path).body_md5() == v3.md5


# ---------- rollback ----------


async def test_rollback_writes_back_and_records(store, repo, prompt_path):
    """验证回滚会回写目标内容并额外记录一条来源明确的新版本。

    参数：
        store: StrategyStore，策略版本管理对象
        repo: Repo，临时数据库仓储
        prompt_path: Path，当前策略书文件路径

    返回：
        None，通过断言验证回写内容、回滚元数据、摘要与版本倒序
    """
    v1 = await store.seed_if_empty()
    v2 = await store.revise(_NEW, "改进", "review_agent")
    v3 = await store.rollback(v1.id)
    assert prompt_path.read_text(encoding="utf-8") == v1.content  # 内容回写
    assert v3.created_by == "rollback" and v3.reason == f"回滚到 v{v1.id}"
    assert v3.md5 == v1.md5  # 回写内容与原版本同 md5
    versions = await repo.review.list_strategy_versions()
    assert [v.id for v in versions] == [v3.id, v2.id, v1.id]  # 最新在前


async def test_rollback_missing_version(store):
    """验证回滚不存在的版本编号时返回明确的策略校验错误。

    参数：
        store: StrategyStore，策略版本管理对象

    返回：
        None，通过断言验证异常消息包含版本不存在提示
    """
    with pytest.raises(StrategyValidationError, match="不存在"):
        await store.rollback(999)


# ---------- 结构化判定字段 no_diff_only ----------


async def test_validation_error_no_diff_only_flag(store, repo, prompt_path):
    """验证 no_diff_only(仅无差异)只在唯一原因确为无差异时成立。

    参数：
        store: StrategyStore，策略版本管理对象
        repo: Repo，临时数据库仓储，本用例通过 store 间接使用
        prompt_path: Path，当前策略书文件路径，本用例通过 store 间接使用

    返回：
        None，通过断言区分无差异、过短和版本不存在三种校验错误
    """
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
    """验证无差异与过短并存时不把混合错误误判为仅无差异。

    参数：
        tmp_path: Path，pytest 临时目录
        repo: Repo，临时数据库仓储

    返回：
        None，通过断言验证两个原因同时存在且 no_diff_only 为 False
    """
    path = tmp_path / "system_prompt.md"
    path.write_text("短内容", encoding="utf-8")
    store = StrategyStore(path, repo)
    with pytest.raises(StrategyValidationError) as exc_info:
        await store.revise("短内容", "r", "review_agent")
    assert len(exc_info.value.reasons) == 2
    assert exc_info.value.no_diff_only is False


# ---------- 变更回调 on_change（WS strategy_updated 接线点） ----------


async def test_on_change_fired_on_revise_and_rollback(prompt_path, repo):
    """验证修订与回滚成功时触发变更回调而播种及校验失败时不触发。

    参数：
        prompt_path: Path，当前策略书文件路径
        repo: Repo，临时数据库仓储

    返回：
        None，通过断言验证不同操作后的累计回调次数
    """
    calls: list[int] = []
    store = StrategyStore(prompt_path, repo, on_change=lambda: calls.append(1))
    v1 = await store.seed_if_empty()
    assert calls == []  # 播种不算变更（启动时无前端需要通知）
    draft = await store.revise(_NEW, "改进", "review_agent")
    assert calls == []  # 草稿落库不算变更（文件未动）
    await store.apply_version(draft.id)
    assert len(calls) == 1
    await store.rollback(v1.id)
    assert len(calls) == 2
    with pytest.raises(StrategyValidationError):
        await store.revise("太短了", "r", "review_agent")  # 校验拒绝：不触发
    with pytest.raises(StrategyValidationError):
        await store.rollback(999)  # 版本不存在：不触发
    assert len(calls) == 2


async def test_on_change_absent_is_noop(store, repo):
    """验证未接入变更回调时策略修订仍可正常完成。

    参数：
        store: StrategyStore，未配置 on_change 的策略版本管理对象
        repo: Repo，临时数据库仓储，本用例通过 store 间接使用

    返回：
        None，通过断言验证新版本摘要与新正文一致
    """
    v = await store.revise(_NEW, "改进", "review_agent")
    assert v.md5 == content_md5(_NEW)
