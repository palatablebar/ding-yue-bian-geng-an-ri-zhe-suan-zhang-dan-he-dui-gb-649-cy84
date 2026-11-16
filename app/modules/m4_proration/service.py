"""M4 按日折算：把每个分段折成金额，再按最大余额分到「分」。

分段金额与抵扣行各自分配一次，但总额只有一个：两者都必须等于
「精确净额四舍五入到分」，否则账单会出现 1 分钱对不平的尾差。
``apply_amounts`` 是纯计算（不落库），落库由 pipeline 单事务完成。
"""
from __future__ import annotations

from decimal import Decimal, localcontext

from ... import config
from . import prorate, rounding


def _sub_index(store, run_id: str) -> dict[str, dict]:
    return {sub["sub_id"]: sub for sub in store.list_subscriptions(run_id)}


def _group(rows: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row["invoice_id"], []).append(row)
    for rows_in_group in grouped.values():
        rows_in_group.sort(key=lambda item: item["seq"])
    return grouped


def apply_amounts(subs: dict[str, dict], segment_rows: list[dict],
                  component_rows: list[dict]) -> dict:
    """把分段与抵扣行的金额算到「分」：就地填 amount_exact/amount_cents/cents。

    幂等：同输入同结果；只改传入的行，不碰存储。
    """
    exact_total = Decimal("0")
    cents_total = 0

    for invoice_id, rows in _group(segment_rows).items():
        sub = subs[rows[0]["sub_id"]]
        period_days = int(sub["period_days"])
        exacts = [
            prorate.segment_amount(Decimal(row["monthly"]), int(row["days"]),
                                   period_days, row["state"])
            for row in rows
        ]
        cents = rounding.allocate(exacts)
        for row, exact, value in zip(rows, exacts, cents):
            row["amount_exact"] = prorate.exact_text(exact)
            row["amount_cents"] = value
            with localcontext() as ctx:
                ctx.prec = config.DECIMAL_PREC
                exact_total += prorate.quantize_exact(exact)
            cents_total += value

    for _invoice_id, rows in _group(component_rows).items():
        exacts = [Decimal(row["exact"]) for row in rows]
        cents = rounding.allocate(exacts)
        for row, value in zip(rows, cents):
            row["cents"] = value

    return {
        "segments": len(segment_rows),
        "components": len(component_rows),
        "segment_exact_total": str(exact_total),
        "segment_cents_total": cents_total,
    }


def segment_rows(store, run_id: str) -> list[dict]:
    """按账单分组的折算明细，页面与接口共用。"""
    subs = _sub_index(store, run_id)
    rows = []
    for segment in store.list_segments(run_id):
        sub = subs[segment["sub_id"]]
        period_days = int(sub["period_days"])
        monthly = Decimal(segment["monthly"] or "0")
        rows.append({
            **segment,
            "account": sub["account"],
            "period_days": period_days,
            "daily": str(prorate.daily_rate(monthly, period_days)) if period_days else "0",
            "amount": rounding.cents_text(segment["amount_cents"]),
        })
    return rows


def summary(store, run_id: str) -> dict:
    rows = segment_rows(store, run_id)
    return {
        "segments": len(rows),
        "invoices": len({row["invoice_id"] for row in rows}),
        "days": sum(int(row["days"]) for row in rows),
        "amount_cents": sum(int(row["amount_cents"]) for row in rows),
        "amount": rounding.cents_text(sum(int(row["amount_cents"]) for row in rows)),
    }
