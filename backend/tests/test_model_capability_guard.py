"""Model-capability guard: a profile model must be able to honour the
configured output-budget policy (output cap and context window)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.ai.router import MODEL_CAPS, validate_model_capability
from app.db import SessionLocal
from app.models import Assessment


@pytest.fixture()
def client(fresh_db):
    from app import main as main_mod

    with TestClient(main_mod.app) as c:
        yield c


def test_shipped_catalogue_passes_both_profiles():
    for model_id in MODEL_CAPS:
        if MODEL_CAPS[model_id][1] >= 128_000:
            assert validate_model_capability(model_id, "reasoner") is None, model_id
    assert validate_model_capability("anthropic/claude-haiku-4.5", "fast") is None


def test_undersized_output_cap_rejected_for_reasoner(monkeypatch):
    monkeypatch.setitem(MODEL_CAPS, "test/tiny-out", (1_000_000, 8_192))
    reason = validate_model_capability("test/tiny-out", "reasoner")
    assert reason is not None and "output tokens" in reason


def test_undersized_context_rejected_for_reasoner(monkeypatch):
    monkeypatch.setitem(MODEL_CAPS, "test/tiny-ctx", (32_768, 128_000))
    reason = validate_model_capability("test/tiny-ctx", "reasoner")
    assert reason is not None and "context window" in reason


def test_unknown_model_warns_but_passes(caplog):
    with caplog.at_level("WARNING"):
        assert validate_model_capability("novel/brand-new-model", "reasoner") is None
    assert any("capability table" in r.message for r in caplog.records)


def test_patch_model_overrides_rejects_known_undersized_model(client, monkeypatch):
    monkeypatch.setitem(MODEL_CAPS, "test/tiny-out", (1_000_000, 8_192))
    with SessionLocal() as db:
        a = Assessment(vendor_name="Acme")
        db.add(a)
        db.commit()
        aid = a.id
    resp = client.patch(
        f"/api/assessments/{aid}/model-overrides",
        json={"gap_analysis": "test/tiny-out"},
    )
    assert resp.status_code == 422
    assert "output tokens" in resp.json()["detail"]

    # Unknown models are accepted (warn-only policy).
    resp = client.patch(
        f"/api/assessments/{aid}/model-overrides",
        json={"gap_analysis": "novel/brand-new-model"},
    )
    assert resp.status_code == 200
