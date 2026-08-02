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
    fired: list = []
    manager = TriggerManager(lambda trigger, price: fired.append((trigger, price)))
    return manager, fired


def test_add_and_list():
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
    manager, _ = make_manager()
    with pytest.raises(ValueError, match="direction"):
        manager.add(BTC, "==", Decimal("70000"))


def test_add_rejects_when_at_max_alerts():
    """全局上限硬校验：第 MAX_ALERTS+1 条拒绝（跨合约合并计数），移除一条后可再设。"""
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
    manager, _ = make_manager()
    t = manager.add(BTC, ">=", Decimal("70000"))
    assert manager.remove(t.id) is True
    assert manager.remove(t.id) is False  # 已移除，再删返回 False
    assert manager.list() == []


def test_find_exact_match():
    manager, _ = make_manager()
    manager.add(BTC, ">=", Decimal("70000"))
    hit = manager.find(BTC, ">=", Decimal("70000"))
    assert hit is not None and hit.price == Decimal("70000")


def test_find_direction_mismatch():
    manager, _ = make_manager()
    manager.add(BTC, ">=", Decimal("70000"))
    assert manager.find(BTC, "<=", Decimal("70000")) is None


def test_find_price_numeric_equivalence():
    """Decimal 数值相等即命中：70000 与 70000.0 视为相同。"""
    manager, _ = make_manager()
    manager.add(BTC, ">=", Decimal("70000"))
    assert manager.find(BTC, ">=", Decimal("70000.0")) is not None


def test_find_scoped_to_contract():
    manager, _ = make_manager()
    manager.add(BTC, ">=", Decimal("70000"))
    assert manager.find(ETH, ">=", Decimal("70000")) is None


def test_fire_gte_when_price_crosses_up():
    manager, fired = make_manager()
    t = manager.add(BTC, ">=", Decimal("70000"))
    assert manager.check(BTC, Decimal("69999")) == []
    assert fired == []
    assert manager.check(BTC, Decimal("70000")) == [t]  # 恰好等于也触发
    assert fired == [(t, Decimal("70000"))]


def test_fire_lte_when_price_crosses_down():
    manager, fired = make_manager()
    t = manager.add(BTC, "<=", Decimal("60000"))
    assert manager.check(BTC, Decimal("60001")) == []
    assert manager.check(BTC, Decimal("59000")) == [t]
    assert fired == [(t, Decimal("59000"))]


def test_fire_only_once():
    manager, fired = make_manager()
    manager.add(BTC, ">=", Decimal("70000"))
    assert len(manager.check(BTC, Decimal("71000"))) == 1
    assert manager.check(BTC, Decimal("72000")) == []  # 已失效，不重复触发
    assert len(fired) == 1
    assert manager.list(BTC) == []


def test_other_contract_not_affected():
    manager, fired = make_manager()
    manager.add(BTC, ">=", Decimal("70000"))
    assert manager.check(ETH, Decimal("999999")) == []
    assert fired == []
    assert len(manager.list(BTC)) == 1


def test_callback_exception_keeps_fire_once_semantics(caplog):
    """回调异常只记日志且不外抛；触发器保持单次触发语义。"""

    def boom(trigger, price):
        raise RuntimeError("回调故障")

    manager = TriggerManager(boom)
    manager.add(BTC, ">=", Decimal("70000"))
    with caplog.at_level(logging.ERROR, logger="src.market.triggers"):
        fired = manager.check(BTC, Decimal("71000"))
    assert len(fired) == 1  # 命中仍返回
    assert manager.list(BTC) == []  # 回调失败也已失效，不会重发
    assert any("回调异常" in r.message for r in caplog.records)


def test_callback_exception_does_not_block_remaining_triggers(caplog):
    """单个回调抛错时本轮其余命中照常派发（不丢失），一次性语义保持。"""
    calls: list[int] = []

    def flaky(trigger, price):
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
