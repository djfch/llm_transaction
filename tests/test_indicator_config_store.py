"""src/review/indicator_config.py 指标短名单版本管理测试：tmp_path 配置文件 + tmp_path 真实 SQLite。

覆盖：校验拒绝（未知键/超 8 个/空 reason/非法字符/与当前无差异）、重复键去重、
revise 成功（原子写文件/版本落库/md5 与文件一致/on_change 触发）、rollback
（回写历史 + 新版本 created_by='rollback'）、seed_if_empty 两分支（无文件播种基线/
有文件记 v1）、load_indicator_config 文件缺失返回默认基线、子仓库直连接入。
"""

import asyncio
import hashlib

import pytest
import yaml

from src.config import DEFAULT_INDICATOR_SHORTLIST, load_indicator_config
from src.memory import Database, Repo
from src.review.indicator_config import IndicatorConfigStore, IndicatorConfigValidationError
from src.review.strategy import content_md5

# 样例合法键集合（真实注册表在 market 层，由外部注入；本层只认注入集合）
VALID_KEYS = frozenset(
    {"ema20", "ema50", "ema200", "rsi14", "macd", "atr14", "oi", "adx14", "vol", "bb20"}
)
_NEW_SHORTLIST = ["rsi14", "adx14"]


@pytest.fixture
async def repo(tmp_path):
    """构造指向临时 SQLite 数据库的 Repo 实例，用毕关闭。

    参数：
        tmp_path: Path，pytest 提供的临时目录
    返回：
        AsyncIterator[Repo]，yield 已打开临时数据库的仓储，并在夹具收尾关闭数据库
    """
    db = Database()
    await db.open(tmp_path / "test.db")
    yield Repo(db)
    await db.close()


@pytest.fixture
def config_path(tmp_path):
    """提供临时目录下的指标配置文件路径（只给路径，不创建文件）。

    参数：
        tmp_path: Path，pytest 提供的临时目录
    返回：
        Path，临时目录下的指标配置文件路径
    """
    return tmp_path / "indicator_config.yaml"


@pytest.fixture
async def store(config_path, repo):
    """构造接入临时配置文件与临时仓储的 IndicatorConfigStore。

    参数：
        config_path: Path，指标配置文件路径
        repo: Repo，测试数据库仓库
    返回：
        IndicatorConfigStore，接入临时文件与仓库的指标配置存储
    """
    return IndicatorConfigStore(config_path, repo, VALID_KEYS)


def _file_shortlist(path) -> list[str]:
    """读取指标配置文件并解析出 shortlist 列表。

    参数：
        path: object，配置文件路径
    返回：
        list[str]，返回该测试辅助函数构造或记录的结果
    """
    return yaml.safe_load(path.read_text(encoding="utf-8"))["shortlist"]


# ---------- 校验拒绝 ----------


async def test_revise_rejects_unknown_key(store, repo, config_path):
    """校验短名单含未知指标键时 revise 被拒绝，不落盘也不落版本。

    参数：
        store: IndicatorConfigStore，指标配置存储测试夹具
        repo: Repo，测试数据库仓库
        config_path: Path，指标配置文件路径
    返回：
        None，执行断言验证目标行为
    """
    with pytest.raises(IndicatorConfigValidationError) as exc_info:
        await store.revise(["ema20", "nope"], "review_agent", "换指标")
    assert any("未知指标键" in r and "nope" in r for r in exc_info.value.reasons)
    assert not config_path.exists()  # 原文件不动（本例中文件尚未创建）
    assert await repo.indicator_config.list_versions() == []  # 无新版本


async def test_revise_rejects_too_many(store, repo):
    """校验短名单超过 8 个指标时 revise 被拒绝。

    参数：
        store: IndicatorConfigStore，指标配置存储测试夹具
        repo: Repo，测试数据库仓库
    返回：
        None，执行断言验证目标行为
    """
    nine = ["ema20", "ema50", "ema200", "rsi14", "macd", "atr14", "oi", "adx14", "vol"]
    with pytest.raises(IndicatorConfigValidationError) as exc_info:
        await store.revise(nine, "review_agent", "加太多")
    assert any("1~8 个" in r for r in exc_info.value.reasons)
    assert await repo.indicator_config.list_versions() == []


async def test_revise_rejects_empty_reason(store, repo, config_path):
    """校验 reason 去空白后为空时 revise 被拒绝。

    参数：
        store: IndicatorConfigStore，指标配置存储测试夹具
        repo: Repo，测试数据库仓库
        config_path: Path，指标配置文件路径
    返回：
        None，执行断言验证目标行为
    """
    with pytest.raises(IndicatorConfigValidationError) as exc_info:
        await store.revise(_NEW_SHORTLIST, "review_agent", "   ")  # strip 后为空
    assert any("reason 不能为空" in r for r in exc_info.value.reasons)
    assert not config_path.exists()
    assert await repo.indicator_config.list_versions() == []


async def test_revise_rejects_bad_charset(store, repo):
    """形状校验在 config 层：大写/非法字符键被拒绝（不进 valid_keys 判定）。

    参数：
        store: IndicatorConfigStore，指标配置存储测试夹具
        repo: Repo，测试数据库仓库
    返回：
        None，执行断言验证目标行为
    """
    with pytest.raises(IndicatorConfigValidationError) as exc_info:
        await store.revise(["EMA20"], "review_agent", "大写键")
    assert any("小写字母/数字/下划线" in r for r in exc_info.value.reasons)
    assert await repo.indicator_config.list_versions() == []


async def test_revise_rejects_no_diff(store, repo, config_path):
    """校验新短名单与当前配置无差异时 revise 被拒绝（标记 no_diff_only）。

    参数：
        store: IndicatorConfigStore，指标配置存储测试夹具
        repo: Repo，测试数据库仓库
        config_path: Path，指标配置文件路径
    返回：
        None，执行断言验证目标行为
    """
    await store.seed_if_empty()  # 当前配置 = 默认基线
    with pytest.raises(IndicatorConfigValidationError) as exc_info:
        await store.revise(list(DEFAULT_INDICATOR_SHORTLIST), "review_agent", "原样提交")
    assert exc_info.value.reasons == ["与当前指标短名单无差异"]
    assert exc_info.value.no_diff_only is True
    assert len(await repo.indicator_config.list_versions()) == 1  # 只有播种的 v1


async def test_revise_dedupes_keys(store, repo, config_path):
    """重复键去重保序后照常落盘/落版本。

    参数：
        store: IndicatorConfigStore，指标配置存储测试夹具
        repo: Repo，测试数据库仓库
        config_path: Path，指标配置文件路径
    返回：
        None，执行断言验证目标行为
    """
    v = await store.revise(["rsi14", "ema20", "rsi14"], "review_agent", "带重复键")
    await store.apply_version(v.id)  # 草稿生效后才写文件（issue #62/#73）
    assert _file_shortlist(config_path) == ["rsi14", "ema20"]
    assert store.load_current().shortlist == ["rsi14", "ema20"]
    assert v.md5 == content_md5(config_path.read_text(encoding="utf-8"))


# ---------- revise 成功 ----------


async def test_revise_success_atomic_persist_and_notify(config_path, repo):
    """校验 revise 成功路径：原子写文件、版本落库、md5 与文件一致、on_change 触发一次。

    参数：
        config_path: Path，指标配置文件路径
        repo: Repo，测试数据库仓库
    返回：
        None，执行断言验证目标行为
    """
    calls: list[int] = []
    store = IndicatorConfigStore(config_path, repo, VALID_KEYS, on_change=lambda: calls.append(1))
    v = await store.revise(_NEW_SHORTLIST, "review_agent", "复盘改进", report_id=7)
    await store.apply_version(v.id)  # 草稿生效后才写文件（issue #62/#73）
    content = config_path.read_text(encoding="utf-8")
    assert v.content == content  # 版本行 content 与落盘文本同源
    assert v.md5 == hashlib.md5(config_path.read_bytes()).hexdigest()  # md5 与文件一致
    assert v.created_by == "review_agent" and v.reason == "复盘改进" and v.report_id == 7
    assert not config_path.with_suffix(".tmp").exists()  # 原子替换后无临时文件残留
    assert calls == [1]  # on_change 触发一次
    assert await repo.indicator_config.latest_md5() == v.md5
    assert _file_shortlist(config_path) == _NEW_SHORTLIST


# ---------- rollback ----------


async def test_rollback_writes_back_and_records(store, repo, config_path):
    """校验 rollback 把历史版本内容回写文件，并新增一条 created_by='rollback' 的版本。

    参数：
        store: IndicatorConfigStore，指标配置存储测试夹具
        repo: Repo，测试数据库仓库
        config_path: Path，指标配置文件路径
    返回：
        None，执行断言验证目标行为
    """
    v1 = await store.seed_if_empty()
    v2 = await store.revise(_NEW_SHORTLIST, "review_agent", "改进")
    v3 = await store.rollback(v1.id)
    assert config_path.read_text(encoding="utf-8") == v1.content  # 内容回写
    assert v3.created_by == "rollback" and v3.reason == f"回滚到 v{v1.id}"
    assert v3.md5 == v1.md5  # 回写内容与原版本同 md5
    versions = await repo.indicator_config.list_versions()
    assert [v.id for v in versions] == [v3.id, v2.id, v1.id]  # 最新在前


async def test_rollback_missing_version(store):
    """验证回滚不存在的指标配置版本时会被拒绝。

    参数：
        store: IndicatorConfigStore，指标配置存储测试夹具
    返回：
        None，执行断言验证目标行为
    """
    with pytest.raises(IndicatorConfigValidationError, match="不存在"):
        await store.rollback(999)


# ---------- 播种 ----------


async def test_seed_creates_baseline_when_file_missing(store, repo, config_path):
    """验证配置文件缺失时会创建基线指标配置版本。

    参数：
        store: IndicatorConfigStore，指标配置存储测试夹具
        repo: Repo，测试数据库仓库
        config_path: Path，指标配置文件路径
    返回：
        None，执行断言验证目标行为
    """
    v = await store.seed_if_empty()
    assert v is not None
    assert v.id == 1 and v.created_by == "human" and v.reason == "初始基线"
    assert config_path.exists()  # 无文件时用默认基线写文件
    assert _file_shortlist(config_path) == DEFAULT_INDICATOR_SHORTLIST
    assert v.md5 == hashlib.md5(config_path.read_bytes()).hexdigest()
    # 幂等：版本表非空后不再播种
    assert await store.seed_if_empty() is None
    assert len(await repo.indicator_config.list_versions()) == 1


async def test_seed_uses_existing_file(store, repo, config_path):
    """验证初始化基线时会采用现有指标配置文件。

    参数：
        store: IndicatorConfigStore，指标配置存储测试夹具
        repo: Repo，测试数据库仓库
        config_path: Path，指标配置文件路径
    返回：
        None，执行断言验证目标行为
    """
    config_path.write_text("shortlist:\n- adx14\n", encoding="utf-8")
    v = await store.seed_if_empty()
    assert v is not None and v.id == 1 and v.created_by == "human"
    assert v.content == "shortlist:\n- adx14\n"  # 以文件原文记 v1
    assert config_path.read_text(encoding="utf-8") == v.content  # 文件不动
    assert store.load_current().shortlist == ["adx14"]


# ---------- load_indicator_config 默认基线 ----------


async def test_load_indicator_config_missing_returns_default(tmp_path):
    """验证指标配置文件缺失时会返回默认配置。

    参数：
        tmp_path: Path，pytest 提供的临时目录
    返回：
        None，执行断言验证目标行为
    """
    cfg = load_indicator_config(tmp_path / "nope.yaml")
    assert cfg.shortlist == DEFAULT_INDICATOR_SHORTLIST


# ---------- 子仓库直连接入 ----------


async def test_store_accepts_sub_repo_directly(config_path, repo):
    """构造参数 repo 也接受子仓库本身（细粒度接线/测试用）。

    参数：
        config_path: Path，指标配置文件路径
        repo: Repo，测试数据库仓库
    返回：
        None，执行断言验证目标行为
    """
    store = IndicatorConfigStore(config_path, repo.indicator_config, VALID_KEYS)
    v = await store.revise(_NEW_SHORTLIST, "human", "直连子仓库")
    await store.apply_version(v.id)  # 草稿生效后才写文件（issue #62/#73）
    assert v.md5 == content_md5(config_path.read_text(encoding="utf-8"))


# ---------- on_change 不触发的路径 ----------


async def test_on_change_not_fired_on_validation_failure(config_path, repo):
    """验证配置校验失败时不会触发变更回调。

    参数：
        config_path: Path，指标配置文件路径
        repo: Repo，测试数据库仓库
    返回：
        None，执行断言验证目标行为
    """
    calls: list[int] = []
    store = IndicatorConfigStore(config_path, repo, VALID_KEYS, on_change=lambda: calls.append(1))
    v1 = await store.seed_if_empty()
    assert calls == []  # 播种不算变更
    draft = await store.revise(_NEW_SHORTLIST, "review_agent", "改进")
    assert calls == []  # 草稿落库不算变更（文件未动）
    await store.apply_version(draft.id)
    assert len(calls) == 1
    await store.rollback(v1.id)
    assert len(calls) == 2
    with pytest.raises(IndicatorConfigValidationError):
        await store.revise(["nope"], "review_agent", "未知键")  # 校验拒绝：不触发
    with pytest.raises(IndicatorConfigValidationError):
        await store.rollback(999)  # 版本不存在：不触发
    assert len(calls) == 2


# ---------- repo 子仓库：版本↔报告关联 ----------


async def test_attach_report_to_version(repo):
    """版本先落库、报告后落库：attach_report_to_version 回填 report_id（对齐策略版本模式）。

    参数：
        repo: Repo，测试数据库仓库
    返回：
        None，执行断言验证目标行为
    """
    v = await repo.indicator_config.save_version("shortlist: [ema20]", "md5-1", "review_agent", "r")
    assert v.report_id is None
    await repo.indicator_config.attach_report_to_version(v.id, 7)
    assert (await repo.indicator_config.get_version(v.id)).report_id == 7


# ---------- 生效锁取代检测（issue #113 F11） ----------


async def test_apply_version_yields_to_newer_applied(store, repo, config_path):
    """旧草稿生效时若已存在更高 id 的 applied 版本，则被取代置 discarded 且不覆盖文件。

    参数：
        store: IndicatorConfigStore，指标配置存储测试夹具
        repo: Repo，测试数据库仓库
        config_path: Path，指标配置文件路径
    返回：
        None，断言 apply 返回 None、文件保留新版本内容、旧草稿状态为 discarded
    """
    await store.seed_if_empty()
    older = await store.revise(["rsi14"], "review_agent", "第一版草稿")
    newer = await store.revise(["adx14"], "review_agent", "第二版草稿")
    applied_newer = await store.apply_version(newer.id)
    assert applied_newer is not None and applied_newer.status == "applied"
    applied_older = await store.apply_version(older.id)
    assert applied_older is None  # 已被更高 applied 版本取代
    assert _file_shortlist(config_path) == ["adx14"]  # 文件保留新版本内容
    assert (await store.get_version(older.id)).status == "discarded"


async def test_rollback_and_apply_interleave_keeps_file_consistent(
    store, repo, config_path, monkeypatch
):
    """rollback 与 apply_version 并发时全程互斥：文件始终等于库内最新 applied 版本内容。

    在 rollback 记新版本前插入延时制造确定性交错：无锁时 apply_version 会插队先生效
    草稿，rollback 随后落库更高 id 的回滚版本而文件停在草稿内容；rollback 收进
    生效锁后两者串行，文件与库内最新 applied 版本必然一致（issue #113 R7）。

    参数：
        store: IndicatorConfigStore，指标配置存储测试夹具
        repo: Repo，测试数据库仓库
        config_path: Path，指标配置文件路径
        monkeypatch: MonkeyPatch，用于给记版本方法插入延时

    返回：
        None，断言文件内容与库内最新 applied 版本内容一致
    """
    await store.seed_if_empty()
    v1 = (await store.list_versions())[0]
    draft = await store.revise(_NEW_SHORTLIST, "review_agent", "复盘改进")
    original_save = repo.indicator_config.save_version

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

    monkeypatch.setattr(repo.indicator_config, "save_version", slow_save)
    await asyncio.gather(store.rollback(v1.id), store.apply_version(draft.id))
    latest = await repo.indicator_config.latest_applied_version()
    assert latest is not None
    assert config_path.read_text(encoding="utf-8") == latest.content


# ---------- 草稿基线 CAS（issue #113） ----------


async def test_cas_discards_stale_draft_after_human_intervenes(store, repo, config_path, caplog):
    """交错复现竞态：人工短名单生效后，基于旧内容的 agent 草稿被 CAS 废弃不覆盖。

    人工路径是两步（revise 落草稿 + apply_version 生效）：人工草稿 C(v2) 生效后，
    轮末再生效基于 v1 基线的 agent 草稿 B(v3)；无 CAS 时旧 id 比较（v3 > v2）
    不会判取代，陈旧 B 会覆盖人工 C（PR #114 R6）。

    参数：
        store: IndicatorConfigStore，指标配置存储测试夹具
        repo: Repo，测试数据库仓库
        config_path: Path，指标配置文件路径
        caplog: LogCaptureFixture，用于断言基线取代告警

    返回：
        None，断言草稿 apply 返回 None、置 discarded、文件保留人工内容、告警落日志
    """
    import logging

    v1 = await store.seed_if_empty()
    human_draft = await store.revise(["adx14"], "human", "人工换指标")
    agent_draft = await store.revise(_NEW_SHORTLIST, "review_agent", "复盘改进", base_md5=v1.md5)
    human = await store.apply_version(human_draft.id)
    assert human is not None and human.status == "applied"
    with caplog.at_level(logging.WARNING):
        applied = await store.apply_version(agent_draft.id)
    assert applied is None  # 基线已失效（人工变更取代实读基线）
    assert (await store.get_version(agent_draft.id)).status == "discarded"
    assert _file_shortlist(config_path) == ["adx14"]  # 文件保留人工内容
    assert any("基线已失效" in r.message for r in caplog.records)


async def test_cas_applies_draft_when_base_matches(store, repo, config_path):
    """草稿基线与最新 applied 内容一致时正常生效（CAS 正路径）。

    参数：
        store: IndicatorConfigStore，指标配置存储测试夹具
        repo: Repo，测试数据库仓库
        config_path: Path，指标配置文件路径

    返回：
        None，断言草稿生效、文件替换、版本行 base_md5 落库
    """
    v1 = await store.seed_if_empty()
    draft = await store.revise(_NEW_SHORTLIST, "review_agent", "复盘改进", base_md5=v1.md5)
    assert draft.base_md5 == v1.md5
    applied = await store.apply_version(draft.id)
    assert applied is not None and applied.status == "applied"
    assert _file_shortlist(config_path) == _NEW_SHORTLIST


async def test_cas_null_base_falls_back_to_id_compare(store, repo, config_path):
    """base_md5 为 NULL 的历史/人工行回退旧 id 比较：id 更大的草稿照常生效。

    参数：
        store: IndicatorConfigStore，指标配置存储测试夹具
        repo: Repo，测试数据库仓库
        config_path: Path，指标配置文件路径

    返回：
        None，断言无基线草稿按旧行为生效覆盖、文件为新短名单
    """
    await store.seed_if_empty()
    v2 = await store.revise(["adx14"], "human", "人工换指标")
    await store.apply_version(v2.id)
    draft = await store.revise(_NEW_SHORTLIST, "review_agent", "复盘改进")  # 不盖基线章
    assert draft.base_md5 is None
    applied = await store.apply_version(draft.id)
    assert applied is not None and applied.status == "applied"  # id 更大，按旧行为生效
    assert _file_shortlist(config_path) == _NEW_SHORTLIST


async def test_cas_replay_of_applied_draft_is_idempotent(store, repo, config_path):
    """已生效草稿的幂等重放不被 CAS 误判：latest 即本版本自身时跳过比对。

    参数：
        store: IndicatorConfigStore，指标配置存储测试夹具
        repo: Repo，测试数据库仓库
        config_path: Path，指标配置文件路径

    返回：
        None，断言二次 apply 仍返回生效版本、文件与状态保持不变
    """
    v1 = await store.seed_if_empty()
    draft = await store.revise(_NEW_SHORTLIST, "review_agent", "复盘改进", base_md5=v1.md5)
    first = await store.apply_version(draft.id)
    assert first is not None and first.status == "applied"
    second = await store.apply_version(draft.id)  # 幂等重放
    assert second is not None and second.status == "applied"
    assert _file_shortlist(config_path) == _NEW_SHORTLIST
    assert (await store.get_version(draft.id)).status == "applied"


async def test_sample_current_base(store, repo, config_path):
    """读取时点基线采样（issue #113 R6-3）：返回（文件正文, 正文 md5, 最新 applied id）三元组。

    参数：
        store: IndicatorConfigStore，指标配置存储测试夹具
        repo: Repo，测试数据库仓库
        config_path: Path，指标配置文件路径

    返回：
        None，断言无文件、有文件无版本、有 applied 三种状态下的采样三元组
    """
    assert await store.sample_current_base() == ("", content_md5(""), None)  # 无文件
    raw = "shortlist:\n- adx14\n"
    config_path.write_text(raw, encoding="utf-8")
    assert await store.sample_current_base() == (raw, content_md5(raw), None)  # 有文件无版本
    v1 = await store.seed_if_empty()
    assert await store.sample_current_base() == (raw, v1.md5, v1.id)  # 有 applied


async def test_cas_discards_draft_when_base_version_aba_replaced(store, repo, config_path, caplog):
    """基线版本 ABA 回绕：草稿基于 v1 实读盖章后，人工改短名单(v2) 又回滚 v1(v3)，轮末废弃。

    内容回绕后当前文件 md5 与基线一致，仅凭 md5 比对的旧 CAS 会放行；新 CAS 按
    实读基线身份检出失效（base_applied_version_id=v1 ≠ 当前生效 v3，issue #113 R6-3）。

    参数：
        store: IndicatorConfigStore，指标配置存储测试夹具
        repo: Repo，测试数据库仓库，本用例通过 store 间接使用
        config_path: Path，指标配置文件路径
        caplog: LogCaptureFixture，用于断言基线失效告警

    返回：
        None，断言草稿 apply 返回 None、置 discarded、文件保留回滚后内容、告警落日志
    """
    import logging

    v1 = await store.seed_if_empty()  # 文件不存在：默认基线落文件并记 v1
    _, base_md5, base_vid = await store.sample_current_base()
    draft = await store.revise(
        _NEW_SHORTLIST,
        "review_agent",
        "复盘改进",
        base_md5=base_md5,
        base_applied_version_id=base_vid,
    )
    human_draft = await store.revise(["adx14"], "human", "人工换指标")  # v2 draft
    assert await store.apply_version(human_draft.id) is not None  # v2 applied
    await store.rollback(v1.id)  # v3：内容回绕为默认短名单，id 前进
    with caplog.at_level(logging.WARNING):
        applied = await store.apply_version(draft.id)
    assert applied is None
    assert (await store.get_version(draft.id)).status == "discarded"
    assert _file_shortlist(config_path) == list(DEFAULT_INDICATOR_SHORTLIST)  # 回滚后内容
    assert any("基线已失效" in r.message and "已不在位" in r.message for r in caplog.records)


async def test_cas_discards_draft_when_file_hot_edited(store, repo, config_path, caplog):
    """文件热编辑：草稿盖章后文件被绕过 store 直接改写（库中 applied 不动），轮末废弃。

    实读基线身份仍在位（v1），但当前文件内容 md5 已偏离实读基线——防"库不变、
    文件被外部编辑"的漏检（issue #113 R6-3）。

    参数：
        store: IndicatorConfigStore，指标配置存储测试夹具
        repo: Repo，测试数据库仓库，本用例通过 store 间接使用
        config_path: Path，指标配置文件路径
        caplog: LogCaptureFixture，用于断言基线失效告警

    返回：
        None，断言草稿 apply 返回 None、置 discarded、热编辑内容不被覆盖、告警落日志
    """
    import logging

    await store.seed_if_empty()
    _, base_md5, base_vid = await store.sample_current_base()
    draft = await store.revise(
        _NEW_SHORTLIST,
        "review_agent",
        "复盘改进",
        base_md5=base_md5,
        base_applied_version_id=base_vid,
    )
    config_path.write_text("shortlist:\n- adx14\n", encoding="utf-8")  # 热编辑：动文件不动库
    with caplog.at_level(logging.WARNING):
        applied = await store.apply_version(draft.id)
    assert applied is None
    assert (await store.get_version(draft.id)).status == "discarded"
    assert _file_shortlist(config_path) == ["adx14"]  # 热编辑内容不被草稿覆盖
    assert any("基线已失效" in r.message and "偏离实读基线" in r.message for r in caplog.records)


async def test_interrupted_after_write_recovers_on_apply_retry(
    store, repo, config_path, monkeypatch
):
    """写文件成功、置状态前中断的草稿：重试识别恢复态补置 applied，不误判热编辑废弃（R7-1）。

    首轮 apply 在原子写成功后让 set_version_status("applied") 抛错，留下「文件已是
    草稿正文、库内仍是旧 applied + 本草稿 draft」的中断现场；随后经 apply_drafts
    重试（_complete_interrupted 的幂等收尾路径）：身份基线仍在位且文件内容已等于
    草稿正文 → 恢复态放行，幂等重写同内容文件并补置 applied，失败集合保持为空
    （轮末事件 applied=True 名实相符）。

    参数：
        store: IndicatorConfigStore，指标配置存储测试夹具
        repo: Repo，测试数据库仓库，本用例通过 store 间接使用
        config_path: Path，指标配置文件路径
        monkeypatch: MonkeyPatch，用于让首次置 applied 抛错模拟进程中断

    返回：
        None，断言重试后草稿 applied、文件与库内最新 applied 一致、失败集合为空
    """
    from types import SimpleNamespace

    from src.review.drafts import apply_drafts

    await store.seed_if_empty()
    _, base_md5, base_vid = await store.sample_current_base()
    draft = await store.revise(
        _NEW_SHORTLIST,
        "review_agent",
        "复盘改进",
        base_md5=base_md5,
        base_applied_version_id=base_vid,
    )
    original_set = repo.indicator_config.set_version_status
    tripped = {"done": False}

    async def flaky_set_status(version_id, status):
        """首次置 applied 时模拟「写文件已成功、状态未落库」的进程中断。

        参数：
            version_id: int，版本编号（透传）
            status: str，目标状态（透传）

        返回：
            原 set_version_status 的返回（透传）

        异常：
            RuntimeError：首次置 applied 时抛出，模拟置状态前进程中断
        """
        if status == "applied" and not tripped["done"]:
            tripped["done"] = True
            raise RuntimeError("模拟置状态前进程中断")
        return await original_set(version_id, status)

    monkeypatch.setattr(repo.indicator_config, "set_version_status", flaky_set_status)
    with pytest.raises(RuntimeError):
        await store.apply_version(draft.id)
    # 中断现场：文件已是草稿正文，库内仍是 v1 applied + 本草稿 draft
    assert _file_shortlist(config_path) == _NEW_SHORTLIST
    assert (await store.get_version(draft.id)).status == "draft"
    deps = SimpleNamespace(
        store=object(),  # 策略通道无草稿，不会被调用
        strategy_draft_ids=[],
        indicator_config_store=store,
        indicator_draft_ids=[draft.id],
        research_prompt_store=None,
        apply_failed_ids=[],
    )
    await apply_drafts(deps)  # _complete_interrupted 的幂等重试路径
    assert deps.apply_failed_ids == []  # 轮末事件 applied=True 名实相符
    assert (await store.get_version(draft.id)).status == "applied"
    latest = await repo.indicator_config.latest_applied_version()
    assert latest is not None and latest.id == draft.id
    assert _file_shortlist(config_path) == _NEW_SHORTLIST
    assert content_md5(config_path.read_text(encoding="utf-8")) == latest.md5
