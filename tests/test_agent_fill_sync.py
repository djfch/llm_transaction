"""ExchangeFillSync 成交同步器测试：分类矩阵、幂等去重、乱序补正、补漏水线、pnl 回填。

fake REST（FakeRest）模拟 ExchangeRestSource；WS 推送直接调 handle_* 入口。
pnl 回填延迟经 monkeypatch 归零，任务经 gather 排空后断言终态。
"""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from types import SimpleNamespace

from src.agent import fill_sync as fill_sync_module
from src.agent.fill_sync import (
    ExchangeFillSync,
    extract_liquidation_order_id,
    extract_triggered_order_id,
    parse_user_trade,
)
from src.agent.fill_sync_setup import build_trade_sync
from src.config import Settings
from src.gateway.base import ExchangeTrade, PositionCloseRecord
from src.memory.db import Database
from src.memory.fills_repo import ExchangeFillsRepo
from src.memory.repo import Repo

BTC = "BTC_USDT"


class FakeRest:
    """ExchangeRestSource 假实现：预置返回列表，记录 position_close 查询次数。"""

    def __init__(
        self,
        my_trades: list[ExchangeTrade] | None = None,
        position_close: list[PositionCloseRecord] | None = None,
        fail: bool = False,
    ) -> None:
        """初始化假 REST 数据源，预置返回内容并清零查询计数。

        参数：
            my_trades: list[ExchangeTrade] | None，list_my_trades 预置返回的成交列表
            position_close: list[PositionCloseRecord] | None，list_position_close 预置返回的平仓记录
            fail: bool，为 True 时 list_my_trades 抛错模拟 REST 不可用

        返回：
            None，副作用是初始化实例字段（含 position_close_calls 查询计数）
        """
        self.my_trades = my_trades or []
        self.position_close = position_close or []
        self.fail = fail
        self.position_close_calls = 0

    def list_my_trades(self, contract: str | None = None, limit: int = 100) -> list[ExchangeTrade]:
        """返回预置的成交列表副本；fail 模式下抛错模拟 REST 故障。

        参数：
            contract: str | None，合约过滤参数，假实现忽略
            limit: int，返回条数上限参数，假实现忽略

        返回：
            list[ExchangeTrade]：预置成交列表的副本

        异常：
            ConnectionError：构造时 fail=True，模拟 REST 拉取失败
        """
        if self.fail:
            raise ConnectionError("rest down")
        return list(self.my_trades)

    def list_position_close(
        self, contract: str, from_ts: float, to_ts: float
    ) -> list[PositionCloseRecord]:
        """返回预置的平仓记录副本，并累计查询次数供断言核对。

        参数：
            contract: str，合约名，假实现忽略
            from_ts: float，查询起始时间戳，假实现忽略
            to_ts: float，查询结束时间戳，假实现忽略

        返回：
            list[PositionCloseRecord]：预置平仓记录列表的副本
        """
        self.position_close_calls += 1
        return list(self.position_close)


def _raw(tid: str, order_id: str, create_time: float = 1000.0, contract: str = BTC) -> dict:
    """构造一条 Gate user trade 推送的原始字典载荷。

    参数：
        tid: str，交易所成交 id
        order_id: str，关联订单 id
        create_time: float，成交时间戳（秒）
        contract: str，合约名，默认 BTC_USDT

    返回：
        dict：模拟 WS 推送的原始成交字典（size/price/fee 等为字符串）
    """
    return {
        "id": tid,
        "order_id": order_id,
        "contract": contract,
        "size": "1",
        "price": "60000",
        "fee": "0.01",
        "role": "taker",
        "text": "",
        "create_time": create_time,
    }


def _trade(tid: str, order_id: str, create_time: float) -> ExchangeTrade:
    """构造一条 ExchangeTrade 对象，模拟 REST 补漏返回的历史成交。

    参数：
        tid: str，交易所成交 id
        order_id: str，关联订单 id
        create_time: float，成交时间戳（秒）

    返回：
        ExchangeTrade：字段固定的 BTC 假成交（数量 1、价格 60000、手续费 0.01）
    """
    return ExchangeTrade(
        id=tid,
        order_id=order_id,
        contract=BTC,
        size=Decimal(1),
        price=Decimal("60000"),
        fee=Decimal("0.01"),
        create_time=create_time,
    )


async def _make_env(tmp_path, rest: FakeRest | None = None) -> SimpleNamespace:
    """搭建测试环境：临时数据库 + 成交同步器 + 事件收集列表。

    参数：
        tmp_path: Path，pytest 临时目录夹具，SQLite 数据库文件落在其中
        rest: FakeRest | None，假 REST 数据源；缺省时内部自建空数据源

    返回：
        SimpleNamespace：含 db（数据库）、repo（仓储）、sync（成交同步器）、events（事件列表）
    """
    db = Database()
    await db.open(tmp_path / "fills.db")
    repo = Repo(db)
    events: list[dict] = []
    sync = ExchangeFillSync(ExchangeFillsRepo(db), rest or FakeRest(), "testnet", events.append)
    return SimpleNamespace(db=db, repo=repo, sync=sync, events=events)


async def _local_order(
    env: SimpleNamespace, order_id: str, *, is_close: bool = False, trade_source: str = ""
) -> None:
    """向本地订单表写入一笔 LLM 订单，供成交分类命中。

    参数：
        env: SimpleNamespace，_make_env 返回的测试环境
        order_id: str，订单 id
        is_close: bool，是否平仓单（决定 side_size 正负）
        trade_source: str，交易来源标记（如 user_close），空串表示 LLM 下单

    返回：
        None，副作用是向 repo 写入一笔订单记录（round_id 固定 "r-1"）
    """
    await env.repo.save_order(
        order_id=order_id,
        round_id="r-1",
        mode="testnet",
        contract=BTC,
        side_size=Decimal(-1 if is_close else 1),
        is_close=is_close,
        trade_source=trade_source,
    )


async def _drain_tasks(sync: ExchangeFillSync) -> None:
    """排空 pnl 回填任务（测试配合 monkeypatch 把两次查询延迟归零）。

    参数：
    sync: ExchangeFillSync，持有待完成盈亏回填任务的成交同步器

    返回：
    None：等待并清空同步器当前登记的异步回填任务
    """
    await asyncio.gather(*list(sync._tasks), return_exceptions=True)


# ---------- parse / extract 纯函数 ----------


def test_parse_user_trade_prefers_create_time_ms():
    """验证成交推送同时提供秒和毫秒时间戳时优先采用毫秒值。

    参数：
        无

    返回：
        None：通过断言校验目标场景，无返回值
    """
    raw = _raw("7", "o1")
    raw["create_time_ms"] = 1000123  # 毫秒优先：除以 1000
    trade = parse_user_trade(raw)
    assert trade.id == "7" and trade.order_id == "o1"
    assert trade.create_time == 1000.123
    assert trade.size == Decimal(1) and trade.fee == Decimal("0.01")


def test_parse_user_trade_defaults_optional_fields():
    """验证成交推送缺少可选字段时解析器填入安全默认值。

    参数：
        无

    返回：
        None：通过断言校验目标场景，无返回值
    """
    raw = {"id": 8, "contract": BTC, "size": -2, "price": 59000, "create_time": 900}
    trade = parse_user_trade(raw)
    assert trade.id == "8" and trade.order_id == "" and trade.role == "" and trade.fee == 0


def test_extract_triggered_order_id_status_filter():
    """验证自动订单提取器只接收已触发状态并忽略未触发回报。

    参数：
        无

    返回：
        None：通过断言校验目标场景，无返回值
    """
    assert extract_triggered_order_id({"status": "open", "order_id": "1"}) == ""
    assert extract_triggered_order_id({"status": "finished", "order_id": "1"}) == "1"
    assert extract_triggered_order_id({"status": "succeeded", "fired_order_id": "2"}) == "2"
    assert extract_triggered_order_id({"status": "finished"}) == ""


def test_extract_liquidation_order_id():
    """验证强平回报中的订单编号可被正确提取。

    参数：
        无

    返回：
        None：通过断言校验目标场景，无返回值
    """
    assert extract_liquidation_order_id({"order_id": "9"}) == "9"
    assert extract_liquidation_order_id({"id": "10"}) == "10"
    assert extract_liquidation_order_id({}) == ""


# ---------- 分类矩阵（本地订单 > 强平集合 > 自动订单集合 > 未知） ----------


async def test_classify_local_order_user_close(tmp_path):
    """验证本地人工平仓订单被归类为 user_close。

    参数：
        tmp_path: Path，pytest 提供的临时目录夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    env = await _make_env(tmp_path)
    try:
        await _local_order(env, "o1", is_close=True, trade_source="user_close")
        await env.sync.handle_user_trade(_raw("t1", "o1"))
        [trade] = await env.repo.trades_between(0.0, 2000.0)
        assert trade.source == "user_close" and trade.round_id == "r-1"
    finally:
        await env.db.close()


async def test_classify_local_order_open_and_close(tmp_path):
    """验证本地 LLM 开仓单与平仓单分别得到正确来源分类。

    参数：
        tmp_path: Path，pytest 提供的临时目录夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    env = await _make_env(tmp_path)
    try:
        await _local_order(env, "o-open")
        await _local_order(env, "o-close", is_close=True)
        await env.sync.handle_user_trade(_raw("t1", "o-open"))
        await env.sync.handle_user_trade(_raw("t2", "o-close"))
        trades = await env.repo.trades_between(0.0, 2000.0)
        assert [t.source for t in trades] == ["llm_open", "llm_close"]
    finally:
        await env.db.close()


async def test_classify_liquidation_and_tpsl_sets(tmp_path):
    """验证强平与止盈止损订单集合能决定成交来源分类。

    参数：
        tmp_path: Path，pytest 提供的临时目录夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    env = await _make_env(tmp_path)
    try:
        await env.sync.handle_liquidation({"order_id": "o-liq"})
        await env.sync.handle_auto_order({"status": "finished", "order_id": "o-tp"})
        await env.sync.handle_user_trade(_raw("t1", "o-liq"))
        await env.sync.handle_user_trade(_raw("t2", "o-tp"))
        trades = await env.repo.trades_between(0.0, 2000.0)
        assert [t.source for t in trades] == ["liquidation", "tpsl_close"]
    finally:
        await env.db.close()


async def test_classify_unknown_still_persisted_with_event(tmp_path):
    """验证未知来源成交仍会落库并发送成交事件。

    参数：
        tmp_path: Path，pytest 提供的临时目录夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    env = await _make_env(tmp_path)
    try:
        await env.sync.handle_user_trade(_raw("t1", "o-elsewhere"))
        [trade] = await env.repo.trades_between(0.0, 2000.0)
        assert trade.source == "" and trade.round_id == ""
        assert env.events == [{"type": "trades_updated", "data": {"contracts": [BTC], "count": 1}}]
    finally:
        await env.db.close()


async def test_local_order_beats_liquidation_set(tmp_path):
    """同一 order_id 既在本地订单又在强平集合：本地订单分类优先。

    参数：
        tmp_path: Path，pytest 提供的临时目录夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    env = await _make_env(tmp_path)
    try:
        await _local_order(env, "o1", is_close=True)
        await env.sync.handle_liquidation({"order_id": "o1"})
        await env.sync.handle_user_trade(_raw("t1", "o1"))
        [trade] = await env.repo.trades_between(0.0, 2000.0)
        assert trade.source == "llm_close"
    finally:
        await env.db.close()


async def test_local_order_beats_auto_order_set(tmp_path):
    """同一 order_id 既在本地订单又在自动订单集合：本地订单分类优先（变异测试发现的缺口）。

    参数：
        tmp_path: Path，pytest 提供的临时目录夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    env = await _make_env(tmp_path)
    try:
        await _local_order(env, "o1", is_close=True)
        await env.sync.handle_auto_order({"status": "finished", "order_id": "o1"})
        await env.sync.handle_user_trade(_raw("t1", "o1"))
        [trade] = await env.repo.trades_between(0.0, 2000.0)
        assert trade.source == "llm_close"
    finally:
        await env.db.close()


async def test_duplicate_exchange_trade_id_deduped(tmp_path):
    """WS/REST 双通道重复：同 exchange_trade_id 只落一行、只发一次事件。

    参数：
        tmp_path: Path，pytest 提供的临时目录夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    env = await _make_env(tmp_path)
    try:
        await env.sync.handle_user_trade(_raw("t1", "o1"))
        await env.sync.handle_user_trade(_raw("t1", "o1"))
        assert len(await env.repo.trades_between(0.0, 2000.0)) == 1
        assert len(env.events) == 1
    finally:
        await env.db.close()


# ---------- 乱序补正 ----------


async def test_reattribute_out_of_order_tpsl(tmp_path, monkeypatch):
    """成交先于自动订单回报到达：先按未知落库，回报到达后补正 tpsl_close 并再发事件。

    参数：
        tmp_path: Path，pytest 提供的临时目录夹具
        monkeypatch: pytest.MonkeyPatch，pytest 提供的动态补丁夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    monkeypatch.setattr(fill_sync_module, "_FIRST_PNL_DELAY_S", 0)
    monkeypatch.setattr(fill_sync_module, "_RETRY_PNL_DELAY_S", 0)
    rest = FakeRest(
        position_close=[
            PositionCloseRecord(
                time=1000.5, contract=BTC, pnl=Decimal("2"), accum_size=Decimal("1")
            )
        ]
    )
    env = await _make_env(tmp_path, rest)
    try:
        await env.sync.handle_user_trade(_raw("t1", "o-tp"))
        await env.sync.handle_auto_order({"status": "finished", "order_id": "o-tp"})
        await _drain_tasks(env.sync)
        [trade] = await env.repo.trades_between(0.0, 2000.0)
        assert trade.source == "tpsl_close"
        assert trade.pnl == Decimal("2")  # 补正时补做的 pnl 回填已命中
        assert len(env.events) == 3  # 落库 + 补正 + pnl 回填各发一次
    finally:
        await env.db.close()


async def test_reattribute_does_not_override_local_source(tmp_path):
    """本地订单已分类（llm_close）的行不被自动订单回报覆盖。

    参数：
        tmp_path: Path，pytest 提供的临时目录夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    env = await _make_env(tmp_path)
    try:
        await _local_order(env, "o1", is_close=True)
        await env.sync.handle_user_trade(_raw("t1", "o1"))
        await env.sync.handle_auto_order({"status": "finished", "order_id": "o1"})
        [trade] = await env.repo.trades_between(0.0, 2000.0)
        assert trade.source == "llm_close"
        assert len(env.events) == 1  # 无补正、无第二次事件
    finally:
        await env.db.close()


# ---------- catch_up 补漏 ----------


async def test_catch_up_first_start_lookback(tmp_path):
    """首启无水线：回补最近 600 秒；REST 倒序输入按时间正序落库。

    参数：
        tmp_path: Path，pytest 提供的临时目录夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    now = time.time()
    rest = FakeRest(
        my_trades=[
            _trade("t-new", "o2", now - 10),
            _trade("t-mid", "o1", now - 300),
            _trade("t-old", "o0", now - 3600),  # 超出 600s 首启窗口
        ]
    )
    env = await _make_env(tmp_path, rest)
    try:
        await env.sync.catch_up()
        trades = await env.repo.trades_between(0.0, now + 10)
        assert len(trades) == 2  # t-old 超出首启窗口未落库
        assert [t.created_at for t in trades] == [now - 300, now - 10]  # 倒序输入按正序落库
        assert await env.sync._fills.latest_exchange_ts("testnet") == now - 10
        assert [t.source for t in trades] == ["", ""]
        assert len(env.events) == 2
    finally:
        await env.db.close()


async def test_catch_up_watermark_overlap_dedup(tmp_path):
    """有水线：重叠 60s 窗口外的旧成交跳过，窗口内已存在的幂等去重。

    参数：
        tmp_path: Path，pytest 提供的临时目录夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    now = time.time()
    env = await _make_env(tmp_path)
    try:
        await env.sync.handle_user_trade(_raw("t-have", "o1", create_time=now - 30))
        rest = FakeRest(
            my_trades=[
                _trade("t-new", "o2", now - 5),  # 窗口内新成交 → 落库
                _trade("t-have", "o1", now - 30),  # 窗口内重复 → 幂等丢弃
                _trade("t-old", "o0", now - 300),  # 窗口外 → 跳过
            ]
        )
        env.sync._rest = rest
        await env.sync.catch_up()
        trades = await env.repo.trades_between(0.0, now + 10)
        assert len(trades) == 2
        assert len(env.events) == 2  # 首次落库 1 次 + 补漏新增 1 次
    finally:
        await env.db.close()


async def test_catch_up_rest_failure_swallowed(tmp_path):
    """REST 拉取失败只记日志：不抛出，下次启动/重连再试。

    参数：
        tmp_path: Path，pytest 提供的临时目录夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    env = await _make_env(tmp_path, FakeRest(fail=True))
    try:
        await env.sync.catch_up()  # 不抛异常
        assert await env.repo.trades_between(0.0, time.time() + 10) == []
    finally:
        await env.db.close()


# ---------- pnl 回填 ----------


async def test_pnl_backfill_hit_updates_row_and_emits(tmp_path, monkeypatch):
    """验证命中平仓记录后回填已实现盈亏并再次发送更新事件。

    参数：
        tmp_path: Path，pytest 提供的临时目录夹具
        monkeypatch: pytest.MonkeyPatch，pytest 提供的动态补丁夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    monkeypatch.setattr(fill_sync_module, "_FIRST_PNL_DELAY_S", 0)
    monkeypatch.setattr(fill_sync_module, "_RETRY_PNL_DELAY_S", 0)
    rest = FakeRest(
        position_close=[
            PositionCloseRecord(
                time=1000.5, contract=BTC, pnl=Decimal("-7.25"), accum_size=Decimal("1")
            )
        ]
    )
    env = await _make_env(tmp_path, rest)
    try:
        await _local_order(env, "o1", is_close=True)
        await env.sync.handle_user_trade(_raw("t1", "o1"))
        await _drain_tasks(env.sync)
        [trade] = await env.repo.trades_between(0.0, 2000.0)
        assert trade.pnl == Decimal("-7.25")
        assert len(env.events) == 2  # 落库一次 + 回填命中一次
    finally:
        await env.db.close()


async def test_pnl_backfill_miss_twice_keeps_zero(tmp_path, monkeypatch):
    """验证两次查询均未命中平仓记录时成交盈亏保持为零。

    参数：
        tmp_path: Path，pytest 提供的临时目录夹具
        monkeypatch: pytest.MonkeyPatch，pytest 提供的动态补丁夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    monkeypatch.setattr(fill_sync_module, "_FIRST_PNL_DELAY_S", 0)
    monkeypatch.setattr(fill_sync_module, "_RETRY_PNL_DELAY_S", 0)
    rest = FakeRest()  # position_close 恒空
    env = await _make_env(tmp_path, rest)
    try:
        await _local_order(env, "o1", is_close=True)
        await env.sync.handle_user_trade(_raw("t1", "o1"))
        await _drain_tasks(env.sync)
        [trade] = await env.repo.trades_between(0.0, 2000.0)
        assert trade.pnl == 0
        assert rest.position_close_calls == 2  # 两次机会用完即弃
        assert len(env.events) == 1
    finally:
        await env.db.close()


async def test_pnl_record_consumed_once(tmp_path, monkeypatch):
    """同一 position_close 记录只消费一次：第二笔同时刻成交回填不到，保持 0。

    参数：
        tmp_path: Path，pytest 提供的临时目录夹具
        monkeypatch: pytest.MonkeyPatch，pytest 提供的动态补丁夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    monkeypatch.setattr(fill_sync_module, "_FIRST_PNL_DELAY_S", 0)
    monkeypatch.setattr(fill_sync_module, "_RETRY_PNL_DELAY_S", 0)
    rest = FakeRest(
        position_close=[
            PositionCloseRecord(
                time=1000.0, contract=BTC, pnl=Decimal("5"), accum_size=Decimal("1")
            )
        ]
    )
    env = await _make_env(tmp_path, rest)
    try:
        await _local_order(env, "o1", is_close=True)
        await _local_order(env, "o2", is_close=True)
        await env.sync.handle_user_trade(_raw("t1", "o1"))
        await env.sync.handle_user_trade(_raw("t2", "o2"))
        await _drain_tasks(env.sync)
        trades = await env.repo.trades_between(0.0, 2000.0)
        assert sorted(t.pnl for t in trades) == [Decimal(0), Decimal("5")]
    finally:
        await env.db.close()


async def test_pnl_nearest_neighbor_no_swap(tmp_path, monkeypatch):
    """链 A 回归：同合约 2s 内两笔平仓，各自命中时间最近的记录（pnl 不互换）。

    参数：
        tmp_path: Path，pytest 提供的临时目录夹具
        monkeypatch: pytest.MonkeyPatch，pytest 提供的动态补丁夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    monkeypatch.setattr(fill_sync_module, "_FIRST_PNL_DELAY_S", 0)
    monkeypatch.setattr(fill_sync_module, "_RETRY_PNL_DELAY_S", 0)
    rest = FakeRest(
        position_close=[
            PositionCloseRecord(
                time=1000.0, contract=BTC, pnl=Decimal("1"), accum_size=Decimal("1")
            ),
            PositionCloseRecord(
                time=1001.5, contract=BTC, pnl=Decimal("2"), accum_size=Decimal("1")
            ),
        ]
    )
    env = await _make_env(tmp_path, rest)
    try:
        await _local_order(env, "o1", is_close=True)
        await _local_order(env, "o2", is_close=True)
        await env.sync.handle_user_trade(_raw("t1", "o1", 1000.0))
        await env.sync.handle_user_trade(_raw("t2", "o2", 1001.5))
        await _drain_tasks(env.sync)
        trades = await env.repo.trades_between(0.0, 2000.0)
        assert [t.pnl for t in trades] == [Decimal("1"), Decimal("2")]  # 各中各的，不互换
    finally:
        await env.db.close()


async def test_pnl_old_unconsumed_record_not_mismatched(tmp_path, monkeypatch):
    """链 B 回归：旧未消费记录（如网页端手动平仓留下）因下界被排除，不错配给新平仓。

    参数：
        tmp_path: Path，pytest 提供的临时目录夹具
        monkeypatch: pytest.MonkeyPatch，pytest 提供的动态补丁夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    monkeypatch.setattr(fill_sync_module, "_FIRST_PNL_DELAY_S", 0)
    monkeypatch.setattr(fill_sync_module, "_RETRY_PNL_DELAY_S", 0)
    old = PositionCloseRecord(time=800.0, contract=BTC, pnl=Decimal("99"), accum_size=Decimal("1"))
    new = PositionCloseRecord(time=1000.5, contract=BTC, pnl=Decimal("3"), accum_size=Decimal("1"))
    rest = FakeRest(position_close=[old])  # 第一次查询时 Gate 侧只有旧记录
    env = await _make_env(tmp_path, rest)
    try:
        await _local_order(env, "o1", is_close=True)
        await env.sync.handle_user_trade(_raw("t1", "o1", 1000.0))
        rest.position_close.append(new)  # 第二次查询时新记录已生成
        await _drain_tasks(env.sync)
        [trade] = await env.repo.trades_between(0.0, 2000.0)
        assert trade.pnl == Decimal("3")  # 旧记录被下界（fill_ts-5s）排除，第二次命中新记录
    finally:
        await env.db.close()


async def test_aclose_cancels_pending_backfill(tmp_path):
    """aclose 取消未完成的 pnl 回填任务（shutdown 语义），任务集合清空。

    参数：
        tmp_path: Path，pytest 提供的临时目录夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    env = await _make_env(tmp_path)
    try:
        await _local_order(env, "o1", is_close=True)
        await env.sync.handle_user_trade(_raw("t1", "o1"))  # 真实 1.5s 延迟，任务挂起中
        assert len(env.sync._tasks) == 1
        await env.sync.aclose()
        assert env.sync._tasks == set()
    finally:
        await env.db.close()


async def test_safety_net_periodic_catch_up(tmp_path, monkeypatch):
    """低频安全网：间隔到点自动幂等补漏；取消任务即停。

    参数：
        tmp_path: Path，pytest 提供的临时目录夹具
        monkeypatch: pytest.MonkeyPatch，pytest 提供的动态补丁夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    monkeypatch.setattr(fill_sync_module, "_SAFETY_NET_INTERVAL_S", 0.01)
    rest = FakeRest(my_trades=[_trade("t1", "o1", time.time() - 5)])
    calls = 0
    original = rest.list_my_trades

    def counting(contract: str | None = None, limit: int = 100) -> list[ExchangeTrade]:
        """记录安全网补漏调用次数并转发原始成交查询。

        参数：
            contract: str | None，合约名称
            limit: int，单次查询最多返回的成交数量

        返回：
            list[ExchangeTrade]：原始假 REST 接口返回的成交记录
        """
        nonlocal calls
        calls += 1
        return original(contract, limit)

    rest.list_my_trades = counting  # type: ignore[method-assign]
    env = await _make_env(tmp_path, rest)
    try:
        task = asyncio.create_task(env.sync.run_safety_net())
        await asyncio.sleep(0.25)  # Windows 计时器粒度 ~15ms，放宽等待确保 ≥2 个 tick
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert calls >= 2  # 周期触发（调度抖动留裕量）
        # 幂等：同一成交多次补漏仍只落一行
        assert len(await env.repo.trades_between(0.0, time.time() + 10)) == 1
    finally:
        await env.db.close()


# ---------- 装配（fill_sync_setup） ----------


async def test_build_trade_sync_paper_returns_none(tmp_path):
    """验证 paper 模式不会装配真实交易所成交同步器。

    参数：
        tmp_path: Path，pytest 提供的临时目录夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    db = Database()
    await db.open(tmp_path / "a.db")
    try:
        assert build_trade_sync(Settings(), FakeRest(), db, lambda e: None, lambda m: None) is None
    finally:
        await db.close()


async def test_build_trade_sync_testnet_wires_handlers(tmp_path, monkeypatch):
    """验证 testnet 模式装配同步器并注册私有成交与告警处理器。

    参数：
        tmp_path: Path，pytest 提供的临时目录夹具
        monkeypatch: pytest.MonkeyPatch，pytest 提供的动态补丁夹具

    返回：
        None：通过断言校验目标场景，无返回值
    """
    monkeypatch.setenv("GATE_API_KEY", "k")
    monkeypatch.setenv("GATE_API_SECRET", "s")
    db = Database()
    await db.open(tmp_path / "a.db")
    alerts: list[str] = []

    async def _alert(message: str) -> None:
        """收集装配链路发出的告警文本供断言。

        参数：
            message: str，告警消息文本

        返回：
            None：把告警文本追加到 alerts 列表
        """
        alerts.append(message)

    try:
        pair = build_trade_sync(Settings(mode="testnet"), FakeRest(), db, lambda e: None, _alert)
        assert pair is not None
        feed, sync = pair
        assert feed._testnet is True
        assert feed._ws_host == Settings(mode="testnet").gate.testnet_ws_host
        assert feed._on_user_trade == sync.handle_user_trade
        assert feed._on_auto_order == sync.handle_auto_order
        assert feed._on_liquidation == sync.handle_liquidation
        assert feed._on_reconnected == sync.catch_up
        # 错误告警 once：连续异常只发一条 Telegram
        assert feed._on_error is not None
        await feed._on_error("boom1")
        await feed._on_error("boom2")
        assert len(alerts) == 1 and "boom1" in alerts[0]
    finally:
        await db.close()
