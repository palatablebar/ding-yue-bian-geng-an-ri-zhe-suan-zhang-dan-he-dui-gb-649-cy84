"""重算原子性：重复执行不新增扣补；失败整体回滚，上一份完整账单原样保留。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import pipeline
from app.main import create_app
from app.modules.m1_ingest import service as ingest_service
from app.modules.m6_invoices import service as invoice_service


def _derived_snapshot(store, run_id):
    return {
        "segments": store.list_segments(run_id),
        "components": store.list_components(run_id),
        "invoices": store.list_invoices(run_id),
        "reconciliations": store.list_reconciliations(run_id),
        "totals": store.list_totals(run_id),
    }


def test_rebuild_is_idempotent_row_for_row(store, settings, run_id):
    # 重复执行 / 重算：行集逐字不变，不会追加扣补行
    before = _derived_snapshot(store, run_id)
    pipeline.rebuild_derived(store, run_id, settings)
    pipeline.rebuild_derived(store, run_id, settings)
    after = _derived_snapshot(store, run_id)
    assert before == after
    assert len(after["components"]) == len(before["components"])


def test_failed_replace_rolls_back_everything(store, settings, run_id):
    # 写入中途失败：五张派生表同生共死，旧账单一行不少
    before = _derived_snapshot(store, run_id)
    bad_components = [{**row, "exact": row["exact_text"]}
                      for row in before["components"]]
    bad_components[3] = {**bad_components[3], "days": None}  # int(None) 必炸
    with pytest.raises(TypeError):
        store.replace_derived(
            run_id,
            segments=before["segments"],
            components=bad_components,
            invoices=before["invoices"],
            reconciliations=before["reconciliations"],
            totals=before["totals"],
        )
    assert _derived_snapshot(store, run_id) == before


def test_failed_recompute_keeps_previous_bill(store, settings, run_id, monkeypatch):
    # 重算在计算阶段就失败：根本没动库，上一份完整账单原样保留
    before = _derived_snapshot(store, run_id)

    def boom(*args, **kwargs):
        raise RuntimeError("模拟重算中途失败")

    monkeypatch.setattr(invoice_service, "build_all", boom)
    with pytest.raises(RuntimeError):
        pipeline.rebuild_derived(store, run_id, settings)
    assert _derived_snapshot(store, run_id) == before


def test_failed_recompute_endpoint_keeps_bill(store, settings, run_id, monkeypatch):
    before = _derived_snapshot(store, run_id)

    def boom(*args, **kwargs):
        raise RuntimeError("模拟重算中途失败")

    monkeypatch.setattr(invoice_service, "build_all", boom)
    app = create_app(settings)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/api/changes")
        assert response.status_code == 500
        assert _derived_snapshot(store, run_id) == before
        # 账单页与导出仍读到同一份完整结果
        data = client.get("/api/invoices").json()
        assert data["totals"]["invoice_cents"] == before["totals"]["invoice_cents"]
        exported = client.get("/api/export/invoices.csv")
        assert exported.headers["x-row-count"] == before["totals"]["invoices"]


def test_failed_import_marks_run_failed_and_retry_succeeds(store, settings, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("模拟采信中途失败")

    monkeypatch.setattr(pipeline, "rebuild_derived", boom)
    with pytest.raises(RuntimeError):
        ingest_service.import_sample(store, settings)
    failed = store.latest_run()
    assert failed["status"] == "failed"
    assert "RuntimeError" in failed["note"]

    # 重试（同输入）：失败批次不占用 batch_key，另起新批次完整出账
    monkeypatch.undo()
    retried = ingest_service.import_sample(store, settings)
    assert retried["status"] == "done"
    assert retried["run_id"] != failed["run_id"]
    assert len(store.list_invoices(retried["run_id"])) == 10
    # 失败批次留在架上可审计，完整账单来自重试批次
    runs = {run["run_id"]: run["status"] for run in store.list_runs()}
    assert runs[failed["run_id"]] == "failed"
    assert runs[retried["run_id"]] == "done"


def test_failed_import_does_not_hide_previous_bill(store, settings, monkeypatch):
    # 已有完整批次时新采信失败：页面默认仍看到上一份完整账单
    from app.web import Ctx

    first = ingest_service.import_sample(store, settings)
    assert first["status"] == "done"

    def boom(*args, **kwargs):
        raise RuntimeError("模拟采信中途失败")

    monkeypatch.setattr(pipeline, "rebuild_derived", boom)
    with pytest.raises(RuntimeError):
        ingest_service.import_sample(store, settings,
                                     "subscriptions_gbk.csv", "changes_dirty.csv")
    assert store.latest_run()["status"] == "failed"
    ctx = Ctx(settings=settings, store=store)
    current = ctx.current_run()
    assert current["run_id"] == first["run_id"]
    assert current["status"] == "done"
    assert len(store.list_invoices(current["run_id"])) == 10
