"""M4 路由：折算明细与重算。

重算走 pipeline.rebuild_derived：内存算清、单事务整批替换，
失败时旧账单原样保留；页面与接口看到同一份数。
"""
from __future__ import annotations

from fastapi import APIRouter, Request

from ... import pipeline, web
from . import service

router = APIRouter(prefix="/api/proration", tags=["M4 按日折算"])


@router.get("")
def proration(request: Request) -> dict:
    run = web.run_or_404(request)
    store = request.app.state.store
    rows = service.segment_rows(store, run["run_id"])
    return {
        "run_id": run["run_id"],
        "summary": service.summary(store, run["run_id"]),
        "segments": rows,
    }


@router.post("")
def recompute(request: Request) -> dict:
    run = web.run_or_404(request)
    store = request.app.state.store
    settings = request.app.state.settings
    rebuilt = pipeline.rebuild_derived(store, run["run_id"], settings)
    return {
        "run_id": run["run_id"],
        "proration": rebuilt["proration"],
        "invoices": rebuilt["summary"],
        "reconciliations": rebuilt["ledgers"],
    }
