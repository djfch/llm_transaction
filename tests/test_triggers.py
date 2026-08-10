"""TriggerManager 单元测试：越线触发、一次性语义、索引维护、回调防护与 find() 查重。

内存唯一存储语义：不落库、启动不重建（进程重启即失效），不存在任何 DB 接线。
"""

from decimal import Decimal

import logging
import pytest

from src.market.triggers import MAX_ALERTS, TriggerManager

BTC = "BTC_USDT"
ETH = "ETH_USDT"


def make_manager() -> tuple[TriggerManager, list]:
    """构造带触发记录回调的 TriggerManager 及其触发记录列表。

    参数：无

    返回：
        tuple[TriggerManager, list]：(触发器管理器, 触发记录列表)，
        回调会把每次触发的 (trigger, price) 追加到列表中供断言
    """
    fired: list = []
    manager = TriggerManager(lambda trigger, price: fired.append((trigger, price)))
    return manager, fired


def test_add_and_list():
    """校验新增触发器分配自增数字 id，且 list 可按合约过滤。

    参数：无

    返回：
        None，断言三条触发器 id 自增为 1/2/3、created_at 为正浮点时间戳，
        list() 返回全部三条、list(BTC) 只含 BTC 两条、list(ETH) 只含 ETH 一条
    """
    manager, _ = make_manager()
    t1 = manager.add(BTC, ">=", Decimal("70000"))
    t2 = manager.add(BTC, "<=", Decimal("60000"))
    t3 = manager.add(ETH, ">=", Decimal("4000"))
    assert (t1.id, t2.id, t3.id) == (1, 2, 3)  # 自增数字 id（/api/alerts 契约要求数字）
    assert isinstance(t1.created_at, float) and t1.created_at > 0
    assert {t.id for t in manager.list()} == {t1.id, t2.id, t3.id}
    assert {t.id for t in manager.list(BTC)} == {t1.id, t2.id}
    assert [t.id for t in manager.list(ETH)] == [t3.id]


def test_add_invalid_direction():
    """校验非法方向（如 "=="）新增触发器时被拒绝。

    参数：无

    返回：
        None，断言 add 抛出错误信息含 "direction" 的 ValueError
    """
    manager, _ = make_manager()
    with pytest.raises(ValueError, match="direction"):
        manager.add(BTC, "==", Decimal("70000"))


def test_add_rejects_when_at_max_alerts():
    """验证预警按全局数量限制，达到上限后拒绝新增且释放名额后可恢复。

    参数：无

    返回：
        None，通过断言验证跨合约合并计数与名额释放语义
    """
    manager, _ = make_manager()
    for i in range(MAX_ALERTS):
        contract = BTC if i % 2 == 0 else ETH  # 跨合约分布，验证按全局总数计
        manager.add(contract, ">=", Decimal(70000 + i))
    with pytest.raises(ValueError, match="上限"):
        manager.add(BTC, ">=", Decimal("99999"))
    assert len(manager.list()) == MAX_ALERTS
    first = manager.list()[0]
    assert manager.remove(first.id) is True
    manager.add(BTC, ">=", Decimal("99999"))  # 腾出名额后可再设
    assert len(manager.list()) == MAX_ALERTS


def test_remove():
    """校验移除语义：首次删除成功、重复删除返回 False、列表随之清空。

    参数：无

    返回：
        None，断言 remove 首次返回 True、再次删除同一 id 返回 False 且 list() 为空
    """
    manager, _ = make_manager()
    t = manager.add(BTC, ">=", Decimal("70000"))
    assert manager.remove(t.id) is True
    assert manager.remove(t.id) is False  # 已移除，再删返回 False
    assert manager.list() == []


def test_find_exact_match():
    """校验 find 按合约+方向+价格精确命中已设触发器。

    参数：无

    返回：
        None，断言 find 命中且返回触发器的价格等于 70000
    """
    manager, _ = make_manager()
    manager.add(BTC, ">=", Decimal("70000"))
    hit = manager.find(BTC, ">=", Decimal("70000"))
    assert hit is not None and hit.price == Decimal("70000")


def test_find_direction_mismatch():
    """校验方向不一致时 find 不命中（同价异向视为不同触发器）。

    参数：无

    返回：
        None，断言用 "<=" 查询同价格触发器返回 None
    """
    manager, _ = make_manager()
    manager.add(BTC, ">=", Decimal("70000"))
    assert manager.find(BTC, "<=", Decimal("70000")) is None


def test_find_price_numeric_equivalence():
    """验证 Decimal 表示形式不同时只要数值相等便可命中同一预警。

    参数：无

    返回：
        None，通过断言验证 70000 与 70000.0 被视为相同价格
    """
    manager, _ = make_manager()
    manager.add(BTC, ">=", Decimal("70000"))
    assert manager.find(BTC, ">=", Decimal("70000.0")) is not None


def test_find_scoped_to_contract():
    """校验 find 按合约隔离：BTC 的触发器不被 ETH 的查询命中。

    参数：无

    返回：
        None，断言用 ETH 查询同方向同价格的触发器返回 None
    """
    manager, _ = make_manager()
    manager.add(BTC, ">=", Decimal("70000"))
    assert manager.find(ETH, ">=", Decimal("70000")) is None


def test_fire_gte_when_price_crosses_up():
    """校验 ">=" 触发器在价格上穿（含恰好等于）目标价时触发并派发回调。

    参数：无

    返回：
        None，断言 69999 不触发、70000 触发且回调收到 (trigger, 70000)
    """
    manager, fired = make_manager()
    t = manager.add(BTC, ">=", Decimal("70000"))
    assert manager.check(BTC, Decimal("69999")) == []
    assert fired == []
    assert manager.check(BTC, Decimal("70000")) == [t]  # 恰好等于也触发
    assert fired == [(t, Decimal("70000"))]


def test_fire_lte_when_price_crosses_down():
    """校验 "<=" 触发器在价格下破目标价时触发并派发回调。

    参数：无

    返回：
        None，断言 60001 不触发、59000 触发且回调收到 (trigger, 59000)
    """
    manager, fired = make_manager()
    t = manager.add(BTC, "<=", Decimal("60000"))
    assert manager.check(BTC, Decimal("60001")) == []
    assert manager.check(BTC, Decimal("59000")) == [t]
    assert fired == [(t, Decimal("59000"))]


def test_fire_only_once():
    """校验一次性语义：触发后即失效，价格继续同向变动不重复触发。

    参数：无

    返回：
        None，断言首次 check 触发一次、再次 check 返回空、回调仅派发一次且
        触发器已从列表移除
    """
    manager, fired = make_manager()
    manager.add(BTC, ">=", Decimal("70000"))
    assert len(manager.check(BTC, Decimal("71000"))) == 1
    assert manager.check(BTC, Decimal("72000")) == []  # 已失效，不重复触发
    assert len(fired) == 1
    assert manager.list(BTC) == []


def test_other_contract_not_affected():
    """校验触发按合约隔离：ETH 价格变动不影响 BTC 的触发器。

    参数：无

    返回：
        None，断言对 ETH 的 check 不触发任何回调，且 BTC 触发器仍保留在列表中
    """
    manager, fired = make_manager()
    manager.add(BTC, ">=", Decimal("70000"))
    assert manager.check(ETH, Decimal("999999")) == []
    assert fired == []
    assert len(manager.list(BTC)) == 1


def test_callback_exception_keeps_fire_once_semantics(caplog):
    """验证预警回调异常只记录日志且不会破坏单次触发语义。

    参数：
        caplog: LogCaptureFixture，pytest 日志捕获夹具

    返回：
        None，通过断言验证异常被隔离、预警被移除且错误日志已记录
    """

    def boom(trigger, price):
        """模拟执行即失败的预警回调。

        参数：
            trigger: Trigger，当前命中的价格预警
            price: Decimal，触发预警的最新价格

        返回：
            None，本函数始终在返回前抛出异常

        异常：
            RuntimeError: 每次调用都抛出，用于验证回调异常隔离
        """
        raise RuntimeError("回调故障")

    manager = TriggerManager(boom)
    manager.add(BTC, ">=", Decimal("70000"))
    with caplog.at_level(logging.ERROR, logger="src.market.triggers"):
        fired = manager.check(BTC, Decimal("71000"))
    assert len(fired) == 1  # 命中仍返回
    assert manager.list(BTC) == []  # 回调失败也已失效，不会重发
    assert any("回调异常" in r.message for r in caplog.records)


def test_callback_exception_does_not_block_remaining_triggers(caplog):
    """验证单个回调失败不会阻断同轮其余已命中预警的派发。

    参数：
        caplog: LogCaptureFixture，pytest 日志捕获夹具

    返回：
        None，通过断言验证两个命中均被派发、移除并留下错误日志
    """
    calls: list[int] = []

    def flaky(trigger, price):
        """记录回调顺序并仅在首次调用时模拟故障。

        参数：
            trigger: Trigger，当前命中的价格预警
            price: Decimal，触发预警的最新价格，本桩函数不读取该值

        返回：
            None，副作用为把预警编号追加到调用记录

        异常：
            RuntimeError: 首次调用时抛出，用于验证后续回调仍会继续
        """
        calls.append(trigger.id)
        if len(calls) == 1:
            raise RuntimeError("首次回调故障")

    manager = TriggerManager(flaky)
    manager.add(BTC, ">=", Decimal("70000"))
    manager.add(BTC, "<=", Decimal("80000"))
    with caplog.at_level(logging.ERROR, logger="src.market.triggers"):
        fired = manager.check(BTC, Decimal("75000"))
    assert len(fired) == 2
    assert len(calls) == 2  # 其余触发器不丢失
    assert manager.list(BTC) == []
    assert any("回调异常" in r.message for r in caplog.records)
