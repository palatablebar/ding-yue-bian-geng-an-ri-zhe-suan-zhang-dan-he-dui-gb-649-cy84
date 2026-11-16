"""派生数据重算编排：内存算清 → 单事务整批替换。

一条铁律：重算先把分段、抵扣行、账单、四本账、合计全部在内存里算完，
最后才用 ``store.replace_derived`` 一个事务落库。因此：

- 重复执行 / 重算不会新增扣补行（先删后插，行集整体替换，不追加）；
- 任何一步失败，旧派生数据原样保留——账单页与导出看到的永远是上一份
  完整账单，不会出现半批；
- 页面与导出读的是同一批表，结果天然一致。
"""
from __future__ import annotations

from .modules.m4_proration import service as proration_service
from .modules.m5_changes import service as change_service
from .modules.m6_invoices import service as invoice_service


def rebuild_derived(store, run_id: str, settings) -> dict:
    """重建一个批次的全部派生行（分段 → 金额 → 账单 → 对账），单事务落库。"""
    subs = store.list_subscriptions(run_id)
    changes = store.list_changes(run_id)
    run = store.get_run(run_id) or {}
    counters = run.get("counters", {})

    built = change_service.build_rows(subs, changes)
    segment_rows = built["segments"]
    component_rows = built["components"]
    amounts = proration_service.apply_amounts(
        {sub["sub_id"]: sub for sub in subs}, segment_rows, component_rows)
    result = invoice_service.build_all(subs, counters, segment_rows,
                                       component_rows, changes)

    store.replace_derived(
        run_id,
        segments=segment_rows,
        components=component_rows,
        invoices=result["invoices"],
        reconciliations=result["ledgers"],
        totals=result["totals"],
    )
    return {
        "changes": {
            "segments": len(segment_rows),
            "components": len(component_rows),
            "changes_applied": built["changes_applied"],
            "changes_superseded": built["changes_superseded"],
            "invoices": built["invoices"],
        },
        "proration": amounts,
        "invoices": result["invoices"],
        "ledgers": result["ledgers"],
        "totals": result["totals"],
        "summary": invoice_service.summary_of(result),
    }
