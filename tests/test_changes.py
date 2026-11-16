"""M5 变更分段：迟到、同日多次、同一时刻、期间外、期间首日变更。"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import expected
from app.modules.m5_changes import segments as segment_mod


def _sub(store, run_id, sub_id):
    return {row["sub_id"]: row for row in store.list_subscriptions(run_id)}[sub_id]


def _changes(store, run_id, sub_id):
    return [row for row in store.list_changes(run_id) if row["sub_id"] == sub_id]


def test_late_change_applied_and_flagged(run_id, store):
    # TRAP-13：乱序到达的变更仍然生效，只是打标记
    sub = _sub(store, run_id, "SUB-1001")
    changes = _changes(store, run_id, "SUB-1001")
    late = [row for row in changes if "R_LATE_CHANGE" in row["flags"]]
    assert [row["change_id"] for row in late] == ["CHG-0009"]
    assert late[0]["applied"] is True
    built = segment_mod.build_segments(sub, changes)
    assert [item.days for item in built] == expected.SEGMENT_DAYS["INV-202602-SUB-1001"]
    assert [item.start.isoformat() for item in built] == [
        "2026-02-01", "2026-02-03", "2026-02-11"]


def test_same_day_multi_last_write_wins(run_id, store):
    # TRAP-25：同日升级又撤回，日终状态（最后一条）生效；较早一条留档被替代
    sub = _sub(store, run_id, "SUB-1008")
    changes = _changes(store, run_id, "SUB-1008")
    winner = [row for row in changes if row["applied"]]
    loser = [row for row in changes if not row["applied"]]
    assert [row["change_id"] for row in winner] == ["CHG-0004"]
    assert loser[0]["change_id"] == "CHG-0003"
    assert loser[0]["superseded_by"] == "CHG-0004"
    assert "R_SAME_DAY_MULTI" in loser[0]["flags"]
    # 撤回到原价 → 日终状态等于日前状态 → 当天不切段，账单回到原样
    built = segment_mod.build_segments(sub, changes)
    assert [item.days for item in built] == [28]
    assert built[0].plan == "标准版"
    assert built[0].monthly == 1200


def test_same_day_collapse_to_last_state():
    # TRAP-25：同日多条变更且日终状态不同 → 一天只切一刀，用日终状态计价
    sub = {
        "plan": "标准版", "state": "active", "monthly": "1200.00",
        "period_start": "2026-02-01", "period_end": "2026-03-01",
    }

    def change(change_id, hour, price, seq):
        return {
            "applied": True, "local_date": "2026-02-14", "seq": seq,
            "change_id": change_id, "kind": "upgrade" if price > 1200 else "downgrade",
            "new_plan": "专业版", "new_price": str(price),
            "effective_utc": f"2026-02-13T{hour:02d}:00:00+00:00", "flags": [],
        }

    changes = [change("CHG-A", 1, 2400, 0), change("CHG-B", 16, 800, 1)]
    built = segment_mod.build_segments(sub, changes)
    assert [(item.days, item.monthly) for item in built] == [(13, Decimal("1200.00")),
                                                             (15, Decimal("800"))]
    # 与行序无关：打乱输入，结果只由 (UTC, 变更号, 行序) 决定
    again = segment_mod.build_segments(sub, list(reversed(changes)))
    assert [(item.days, item.plan, item.monthly) for item in again] == [
        (item.days, item.plan, item.monthly) for item in built]


def test_same_instant_overlap_deterministic(run_id, store):
    # TRAP-25：同一时刻两条变更，结果只由 (变更号, 行号) 决定，与输入顺序无关
    sub = _sub(store, run_id, "SUB-1015")
    changes = _changes(store, run_id, "SUB-1015")
    assert {row["change_id"] for row in changes if row["applied"]} == {"CHG-0013"}
    forward = segment_mod.build_segments(sub, changes)
    backward = segment_mod.build_segments(sub, list(reversed(changes)))
    assert [(item.days, item.plan) for item in forward] == [
        (item.days, item.plan) for item in backward]
    assert forward[-1].monthly == 300


def test_out_of_period_change_not_applied(run_id, store):
    # TRAP-23：生效日落在期间外的变更不算进本期
    sub = _sub(store, run_id, "SUB-1006")
    changes = _changes(store, run_id, "SUB-1006")
    outside = [row for row in changes if "R_CHANGE_OUT_OF_PERIOD" in row["flags"]]
    assert [row["change_id"] for row in outside] == ["CHG-0011"]
    assert outside[0]["applied"] is False
    built = segment_mod.build_segments(sub, changes)
    assert [item.days for item in built] == [19, 9]
    assert built[-1].state == "paused"


def test_change_on_period_start_zero_day_segment(run_id, store):
    # TRAP-23：期间首日就变更时，首段是零天段；半开区间不重复计入
    sub = _sub(store, run_id, "SUB-1010")
    changes = _changes(store, run_id, "SUB-1010")
    built = segment_mod.build_segments(sub, changes)
    assert [item.days for item in built] == expected.SEGMENT_DAYS["INV-202611-SUB-1010"]
    assert built[0].start == built[0].end == dt.date(2026, 11, 1)
    assert built[0].state == "active"
    assert built[1].state == "paused"
    assert built[2].state == "active"


def test_billable_segments_drop_zero_days(run_id, store):
    sub = _sub(store, run_id, "SUB-1010")
    built = segment_mod.build_segments(sub, _changes(store, run_id, "SUB-1010"))
    billable = segment_mod.billable_segments(built)
    assert [item.days for item in billable] == [14, 16]
    ledger = segment_mod.day_ledger(built)
    assert ledger == {"billed_days": 16, "free_days": 14, "days": 30}
