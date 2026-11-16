"""M5 路由：变更序列、抵扣行、重算。"""
from __future__ import annotations

from fastapi import APIRouter, Request

from ... import pipeline, web
from . import service

router = APIRouter(prefix="/api/changes", tags=["M5 变更抵扣"])


@router.get("")
def changes(request: Request) -> dict:
    run = web.run_or_404(request)
    store = request.app.state.store
    return {
        "run_id": run["run_id"],
        "summary": service.summary(store, run["run_id"]),
        "changes": service.change_rows(store, run["run_id"]),
        "components": service.component_rows(store, run["run_id"]),
    }


@router.post("")
def rebuild(request: Request) -> dict:
    run = web.run_or_404(request)
    store = request.app.state.store
    settings = request.app.state.settings
    rebuilt = pipeline.rebuild_derived(store, run["run_id"], settings)
    return {
        "run_id": run["run_id"],
        "changes": rebuilt["changes"],
        "proration": rebuilt["proration"],
        "invoices": rebuilt["summary"],
        "reconciliations": rebuilt["ledgers"],
    }
