"""操作流水：每条期间内变更「若生效的差额」，只读推导，不记账。

客服要在账单上看到「先升级、又撤回」两次操作各自的差额；财务要的是唯一
最终金额。两者用一条恒等式同时满足：

    整期预缴 + Σ(各操作差额) = Σ(各段费率 × 段天数) = 账单额

差额按操作全序 (本地日, UTC, 变更号, 行序) 链式推进状态：撤回操作的差额
是相对它前面那条升级算出来的，所以同日两条差额互相抵消，合计不变。
操作流水是证据，不落库为钱；账单金额只由分段与抵扣行决定。
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from ... import config
from ..m4_proration import prorate, rounding
from . import segments as segments_mod


def _state_label(plan: str, state: str, monthly: Decimal) -> str:
    label = config.STATE_LABELS.get(state, state)
    return f"{plan} {monthly}（{label}）"


def build_operations(sub: dict, changes: list[dict],
                     period_days: int) -> tuple[list[dict], Decimal]:
    """返回 (操作流水行, 差额合计精确值)；期间外变更不属于这张账单，不出现。

    差额合计用未量化的 Decimal 求和、最后才量化到 8 位，保证
    「预缴 + 差额合计 = 账单额」在入库精度上逐字成立。
    """
    start = dt.date.fromisoformat(sub["period_start"])
    end = dt.date.fromisoformat(sub["period_end"])
    plan, state, monthly = segments_mod.initial_state(sub)
    ordered = sorted(
        (change for change in changes
         if start <= dt.date.fromisoformat(change["local_date"]) < end),
        key=lambda item: (item["local_date"], item["effective_utc"],
                          item["change_id"], item["seq"]),
    )
    operations: list[dict] = []
    net = Decimal("0")
    for change in ordered:
        day = dt.date.fromisoformat(change["local_date"])
        remaining = period_days - (day - start).days
        old_rate = prorate.rate_for(monthly, state)
        new_plan, new_state, new_monthly = segments_mod.apply_change(
            plan, monthly, change)
        new_rate = prorate.rate_for(new_monthly, new_state)
        delta = prorate.share(new_rate - old_rate, remaining, period_days)
        net += delta
        applied = bool(change["applied"])
        operations.append({
            "change_id": change["change_id"],
            "kind": change["kind"],
            "kind_label": config.KIND_LABELS.get(change["kind"], change["kind"]),
            "effective_local": change["effective_local"],
            "local_date": change["local_date"],
            "from_label": _state_label(plan, state, monthly),
            "to_label": _state_label(new_plan, new_state, new_monthly),
            "days": remaining,
            "delta_exact": prorate.exact_text(delta),
            "delta": rounding.cents_text(rounding.to_cents(delta)),
            "disposition": "effective" if applied else "superseded",
            "disposition_label": "生效" if applied else "同日被替代",
            "superseded_by": change.get("superseded_by", ""),
            "flags": list(change["flags"]),
        })
        plan, state, monthly = new_plan, new_state, new_monthly
    return operations, net
