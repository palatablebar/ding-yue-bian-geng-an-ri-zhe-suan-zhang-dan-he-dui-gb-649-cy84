"""操作流水：同日升级又撤回，账单回到原样；两次操作与各自差额都在账单上可见。"""
from __future__ import annotations

from decimal import Decimal

import expected
from app.modules.m5_changes import operations as operations_mod
from app.modules.m6_invoices import service as invoice_service


def test_same_day_revert_bill_returns_to_original(run_id, store):
    # 财务：一天一张可复算账单，最终金额 = 未变更时的原价整期
    invoice = store.get_invoice(run_id, "INV-202602-SUB-1008")
    assert invoice["total_cents"] == 120000
    assert invoice["charge_cents"] == 120000 and invoice["credit_cents"] == 0
    # 当天不产生扣补行：只有整期预缴一条
    components = store.list_components(run_id, "INV-202602-SUB-1008")
    assert [(row["kind"], row["source"]) for row in components] == [("charge", "base")]


def test_same_day_revert_operations_visible_with_deltas(seeded_client):
    # 客服：账单明细里两次操作都在，差额一正一负互相抵消
    data = seeded_client.get("/api/invoices/INV-202602-SUB-1008").json()
    operations = data["operations"]
    assert [row["change_id"] for row in operations] == ["CHG-0003", "CHG-0004"]
    upgrade, revert = operations
    assert upgrade["disposition"] == "superseded"
    assert upgrade["superseded_by"] == "CHG-0004"
    assert upgrade["delta"] == "642.86"
    assert revert["disposition"] == "effective"
    assert revert["delta"] == "-642.86"
    assert data["operations_net_exact"] == "0E-8"
    assert data["invoice"]["amount"] == "1200.00"


def test_operations_identity_holds_per_invoice(run_id, store):
    # 恒等式：整期预缴 + Σ操作差额 = 账单额（入库精度逐字成立）
    for invoice in store.list_invoices(run_id):
        data = invoice_service.detail(store, run_id, invoice["invoice_id"])
        base = [row for row in data["components"] if row["source"] == "base"]
        base_exact = Decimal(base[0]["exact_text"]) if base else Decimal("0")
        net = Decimal(data["operations_net_exact"])
        assert base_exact + net == Decimal(invoice["exact_total"]), invoice["invoice_id"]


def test_operations_chain_follows_change_order(run_id, store):
    # 差额按操作顺序链式推进：先降 1200→400，再升 400→2400
    subs = {row["sub_id"]: row for row in store.list_subscriptions(run_id)}
    changes = [row for row in store.list_changes(run_id) if row["sub_id"] == "SUB-1001"]
    operations, net = operations_mod.build_operations(
        subs["SUB-1001"], changes, subs["SUB-1001"]["period_days"])
    assert [row["change_id"] for row in operations] == ["CHG-0009", "CHG-0001"]
    assert operations[0]["delta_exact"] == "-742.85714286"   # (400-1200)×26/28
    assert operations[1]["delta_exact"] == "1285.71428571"   # (2400-400)×18/28
    assert abs(net - (Decimal("15200") / 28)) < Decimal("1E-20")


def test_out_of_period_change_not_in_operations(run_id, store):
    # 期间外变更不属于本账单的操作流水
    subs = {row["sub_id"]: row for row in store.list_subscriptions(run_id)}
    changes = [row for row in store.list_changes(run_id) if row["sub_id"] == "SUB-1006"]
    operations, _net = operations_mod.build_operations(
        subs["SUB-1006"], changes, subs["SUB-1006"]["period_days"])
    assert [row["change_id"] for row in operations] == ["CHG-0014"]


def test_change_rows_scoped_components(run_id, store):
    # 每条变更只挂自己名下的抵扣行，不再把整张账单的行重复列出
    from app.modules.m5_changes import service as change_service

    rows = change_service.change_rows(store, run_id)
    by_id = {row["change_id"]: row for row in rows}
    own = by_id["CHG-0009"]["components"]
    assert own and all(row["source"] == "change:CHG-0009" for row in own)
    # 同日被替代的变更没有抵扣行，但留档可查
    assert by_id["CHG-0003"]["components"] == []
    assert by_id["CHG-0003"]["superseded_by"] == "CHG-0004"
    # 平铺清单每行只出现一次：总数 = 各账单行数之和
    flat = change_service.component_rows(store, run_id)
    assert len(flat) == expected.COMPONENTS
    assert len({(row["invoice_id"], row["seq"]) for row in flat}) == len(flat)
