"""M6 账单对账：出账、四账核对、合计。

出账只读已归一的订阅与派生行，因此重建一次不会改变输入；
``build_all`` 是纯计算，落库由 pipeline 单事务完成；
同一批次重跑，账单行数与合计逐字不变。
"""
from __future__ import annotations

from ... import config, reasons
from . import invoice as invoice_mod
from . import reconcile


def _group(rows: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row["invoice_id"], []).append(row)
    for group in grouped.values():
        group.sort(key=lambda item: item["seq"])
    return grouped


def _change_flags(sub: dict, changes: list[dict]) -> list[str]:
    """本期间内变更的复核级标记：同日多次、迟到、夏令时等事实随账单留证。"""
    flags: list[str] = []
    for change in changes:
        if not (sub["period_start"] <= change["local_date"] < sub["period_end"]):
            continue
        for code in change["flags"]:
            if reasons.level_of(code) == "warn" and code not in flags:
                flags.append(code)
    return flags


def build_all(subs: list[dict], counters: dict, segment_rows: list[dict],
              component_rows: list[dict], changes: list[dict]) -> dict:
    """纯计算：账单 + 四本账 + 合计（不落库）。"""
    subs_by_id = {sub["sub_id"]: sub for sub in subs}
    changes_by_sub: dict[str, list[dict]] = {}
    for change in changes:
        changes_by_sub.setdefault(change["sub_id"], []).append(change)
    segment_groups = _group(segment_rows)
    component_groups = _group(component_rows)

    invoices: list[dict] = []
    for invoice_id, rows in sorted(segment_groups.items()):
        sub = subs_by_id.get(rows[0]["sub_id"])
        if sub is None:
            continue
        invoices.append(invoice_mod.build_invoice(
            sub, rows, component_groups.get(invoice_id, []),
            change_flags=_change_flags(sub, changes_by_sub.get(sub["sub_id"], []))))

    ledgers = reconcile.build(counters=counters, invoices=invoices,
                              segments=segment_rows, components=component_rows)
    totals = reconcile.totals(counters, invoices, segment_rows, component_rows)
    return {"invoices": invoices, "ledgers": ledgers, "totals": totals}


def summary_of(result: dict) -> dict:
    totals = result["totals"]
    return {
        "invoices": len(result["invoices"]),
        "period_days": totals["period_days"],
        "charge_cents": totals["charge_cents"],
        "credit_cents": totals["credit_cents"],
        "invoice_cents": totals["invoice_cents"],
        "zero_invoices": totals["zero_invoices"],
        "money_unit": config.MONEY_UNIT,
    }


def invoice_rows(store, run_id: str) -> list[dict]:
    from ..m4_proration import rounding

    subs = {sub["sub_id"]: sub for sub in store.list_subscriptions(run_id)}
    rows = []
    for row in store.list_invoices(run_id):
        sub = subs.get(row["sub_id"], {})
        rows.append({
            **row,
            "amount": rounding.cents_text(row["total_cents"]),
            "charge": rounding.cents_text(row["charge_cents"]),
            "credit": rounding.cents_text(row["credit_cents"]),
            "leap": bool(sub.get("leap", 0)),
            "range_label": f"[{row['period_start']}, {row['period_end']})",
        })
    return rows


def detail(store, run_id: str, invoice_id: str) -> dict | None:
    from ..m4_proration import prorate
    from ..m5_changes import operations as operations_mod

    row = store.get_invoice(run_id, invoice_id)
    if row is None:
        return None
    segments = store.list_segments(run_id, invoice_id)
    components = store.list_components(run_id, invoice_id)
    subs = {sub["sub_id"]: sub for sub in store.list_subscriptions(run_id)}
    sub = subs.get(row["sub_id"])
    operations: list[dict] = []
    net_exact = "0"
    if sub is not None:
        own = [change for change in store.list_changes(run_id)
               if change["sub_id"] == row["sub_id"]]
        operations, net = operations_mod.build_operations(sub, own, row["period_days"])
        net_exact = prorate.exact_text(net)
    return invoice_mod.detail(row, segments, components, operations, net_exact)


def overview(store, run_id: str) -> dict:
    return {
        "invoices": invoice_rows(store, run_id),
        "ledgers": store.list_reconciliations(run_id),
        "totals": store.list_totals(run_id),
    }
