"""TriggerManager 单元测试：越线触发、一次性语义、索引维护、回调防护与生命周期接线。"""

import asyncio
import logging
from decimal import Decimal
from pathlib import Path

import pytest

from src.market.triggers import PriceTrigger, TriggerManager, make_fire_callback, rebuild_from_repo
from src.memory.db import Database
from src.memory.repo import Repo

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
    assert t1.id.startswith("trig-")
    assert {t.id for t in manager.list()} == {t1.id, t2.id, t3.id}
    assert {t.id for t in manager.list(BTC)} == {t1.id, t2.id}
    assert [t.id for t in manager.list(ETH)] == [t3.id]


def test_add_invalid_direction():
    manager, _ = make_manager()
    with pytest.raises(ValueError, match="direction"):
        manager.add(BTC, "==", Decimal("70000"))


def test_remove():
    manager, _ = make_manager()
    t = manager.add(BTC, ">=", Decimal("70000"))
    assert manager.remove(t.id) is True
    assert manager.remove(t.id) is False  # 已移除，再删返回 False
    assert manager.list() == []


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
    """回调抛错不再外抛（记日志），但触发器已失效、不会重发。"""

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
    calls: list[str] = []

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


# ---------- 生命周期接线辅助（alerts 表双写） ----------


async def _wait_alert_inactive(repo: Repo) -> None:
    """轮询等待告警行被持久化关闭（deactivate 在后台任务中异步落库）。"""

    async def _poll() -> None:
        while (await repo.list_alerts(active_only=False))[0].active:
            await asyncio.sleep(0)

    await asyncio.wait_for(_poll(), 2)


async def test_make_fire_callback_deactivates_alert_and_wakes(tmp_path: Path):
    """触发回调：匹配 (contract, direction, price) 的 active 告警行置 0，并抢醒调度器。"""
    db = Database()
    await db.open(tmp_path / "t.db")
    repo = Repo(db)
    await repo.add_alert("r1", BTC, "above", Decimal("70000"))
    wakes: list[str] = []
    callback = make_fire_callback(repo, lambda reason: wakes.append(reason) or True)
    trigger = PriceTrigger(id="trig-x", contract=BTC, direction=">=", price=Decimal("70000"))
    callback(trigger, Decimal("71000"))
    assert wakes == [f"price_trigger:{BTC}@71000"]
    await _wait_alert_inactive(repo)
    assert (await repo.list_alerts(active_only=False))[0].active is False
    await db.close()


def test_make_fire_callback_without_loop_skips_persistence():
    """无运行中事件循环（纯同步用法）：跳过持久化，仍正常抢醒、不抛错。"""
    wakes: list[str] = []
    callback = make_fire_callback(None, lambda reason: wakes.append(reason) or True)  # type: ignore[arg-type]
    trigger = PriceTrigger(id="trig-x", contract=BTC, direction=">=", price=Decimal("70000"))
    callback(trigger, Decimal("71000"))  # 同步上下文无事件循环：不应抛错
    assert wakes == [f"price_trigger:{BTC}@71000"]


async def test_rebuild_from_repo_restores_active_alerts(tmp_path: Path):
    """启动重建：active=1 的告警恢复为内存触发器（重启后预警不丢）。"""
    db = Database()
    await db.open(tmp_path / "t.db")
    repo = Repo(db)
    await repo.add_alert("r1", BTC, "above", Decimal("70000"))
    await repo.add_alert("r2", ETH, "below", Decimal("3000"))

    manager, _ = make_manager()
    count = await rebuild_from_repo(repo, manager)
    await db.close()
    assert count == 2
    restored = {(t.contract, t.direction, t.price) for t in manager.list()}
    assert restored == {(BTC, ">=", Decimal("70000")), (ETH, "<=", Decimal("3000"))}


async def test_rebuild_from_repo_skips_inactive_and_unknown_direction(tmp_path: Path, caplog):
    """已关闭与未知方向的告警行不重建（未知方向记告警日志）。"""
    db = Database()
    await db.open(tmp_path / "t.db")
    repo = Repo(db)
    gone = await repo.add_alert("r1", BTC, "above", Decimal("70000"))
    await repo.deactivate_alert(gone.id)
    await repo.add_alert("r2", ETH, "sideways", Decimal("3000"))  # 脏数据

    manager, _ = make_manager()
    with caplog.at_level(logging.WARNING, logger="src.market.triggers"):
        count = await rebuild_from_repo(repo, manager)
    await db.close()
    assert count == 0
    assert manager.list() == []
    assert any("未知方向" in r.message for r in caplog.records)
