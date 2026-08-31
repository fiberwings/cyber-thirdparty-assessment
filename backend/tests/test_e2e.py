"""End-to-end smoke test: exercise every API endpoint against a fake LLM.

The fake LLM is wired in by monkey-patching `OpenRouterClient` inside the
`app.ai.router` module so every agent picks it up without any per-test plumbing.
"""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
from tempfile import NamedTemporaryFile

import httpx
import pymupdf
import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from .conftest import FakeOpenRouterClient


@pytest.fixture()
def patched_client(monkeypatch, fresh_db):
    fake = FakeOpenRouterClient()

    # Sufficient on first scoping turn (skip the loop)
    fake.push_json(
        {
            "is_sufficient": True,
            "sufficiency_breakdown": {
                "data_types": 4, "hosting": 4, "network_access": 4,
                "identity_flow": 4, "regulatory_scope": 4, "geography": 4, "criticality": 4
            },
            "missing_dimensions": [],
            "next_question": None,
            "summary_so_far": "SaaS billing vendor processing EU PII over public APIs.",
        }
    )
    # Per-document weakness extraction (auto-fired on SOC upload). Empty list
    # — clean SOC report. With no extracted weaknesses, the auto-fired
    # cross-correlation step sees nothing to do and exits without a call.
    fake.push_json({"weaknesses": []})
    # Attestation profile (fast call, fired for soc/iso/pentest uploads).
    fake.push_json({
        "doc_type": "soc2_type2", "doc_type_quote": "SOC 2 Type 2 report",
        "period_start": {"value": "2025-01-01", "quote": "period 1 January 2025"},
        "period_end": {"value": "2025-12-31", "quote": "to 31 December 2025"},
        "opinion": {"value": "unqualified", "quote": "in our opinion, controls were suitably designed"},
    })
    # Scenario generation — phase 1 (skeletons)
    fake.push_json(
        {
            "scenarios": [
                {
                    "code": "DATA_LEAK",
                    "name": "Data leakage of customer PII",
                    "description": "Vendor mishandles billing PII.",
                    "inherent_impact": 3,
                    "inherent_likelihood": 3,
                }
            ]
        }
    )
    # Scenario generation — phase 2 (per-scenario controls)
    fake.push_json(
        {
            "expected_controls": [
                {"code": "ENC.REST", "name": "Encryption at rest", "description": "", "weight": 1.0, "rationale": ""},
                {"code": "IAM.MFA", "name": "MFA", "description": "", "weight": 1.0, "rationale": ""},
            ]
        }
    )
    # Gap analysis (whole-bundle mode): 2 controls in ONE batched response
    fake.push_json({"controls": [
        {
            "control_code": "ENC.REST", "coverage": "full", "effectiveness": "strong",
            "citations": [{"document_id": 1, "page": 1, "section_path": "Encryption", "quote": "AES-256 at rest"}],
            "rationale": "AES-256 documented.", "meta_flags": [],
        },
        {
            "control_code": "IAM.MFA", "coverage": "full", "effectiveness": "adequate",
            "citations": [{"document_id": 1, "page": 1, "section_path": "Access", "quote": "Admins must use MFA"}],
            "rationale": "MFA mandated.", "meta_flags": [],
        },
    ]})
    # Weakness synthesize endpoint now aliases cross-correlation. With no
    # extracted weaknesses, the agent returns early without an LLM call —
    # so no fake response is needed for that step.
    # Narratives — one per scenario
    fake.push_json("Residual risk Moderate. Strong encryption; adequate MFA.")

    import app.ai.router as router_mod
    monkeypatch.setattr(router_mod, "OpenRouterClient", lambda *a, **kw: fake)
    return fake


def _make_pdf(text: str) -> bytes:
    pdf = pymupdf.open()
    page = pdf.new_page()
    page.insert_text((72, 72), "Encryption", fontsize=16)
    page.insert_text((72, 110), "AES-256 at rest. TLS 1.2+ in transit.", fontsize=11)
    page.insert_text((72, 150), "Access", fontsize=16)
    page.insert_text((72, 190), "Admins must use MFA. Reviews quarterly.", fontsize=11)
    out = io.BytesIO()
    pdf.save(out)
    pdf.close()
    return out.getvalue()


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_full_flow(patched_client):
    from app.main import app
    client = TestClient(app)

    # 1. Create assessment
    r = client.post("/api/assessments", json={"vendor_name": "Acme Billing"})
    assert r.status_code == 201, r.text
    aid = r.json()["id"]

    # 2. Set description
    r = client.post(f"/api/assessments/{aid}/description",
                    json={"text": "Acme is a SaaS billing vendor processing EU PII via REST APIs."})
    assert r.status_code == 200

    # 3. Scoping turn (background task) → sufficient
    r = client.post(f"/api/assessments/{aid}/scoping/turn", json={"answer": "Looks good."})
    assert r.status_code == 200
    task_id = r.json()["task_id"]
    for _ in range(30):
        s = client.get(f"/api/tasks/{task_id}").json()
        if s["status"] in {"done", "error"}:
            break
        import time; time.sleep(0.2)
    assert s["status"] == "done", s
    r = client.get(f"/api/assessments/{aid}/description")
    assert r.status_code == 200
    assert r.json()["is_sufficient"] is True

    # 4. Upload a SOC 2 PDF — auto-triggers per-document weakness extraction.
    pdf_bytes = _make_pdf("body")
    r = client.post(
        f"/api/assessments/{aid}/documents",
        data={"kind": "soc"},
        files={"file": ("soc2.pdf", pdf_bytes, "application/pdf")},
    )
    assert r.status_code == 201, r.text
    assert r.json()["filename"] == "soc2.pdf"
    # Wait for the auto-fired extraction task before moving on so that the
    # background task doesn't race with subsequent test steps for fake-LLM
    # canned responses.
    weakness_task_id = r.json().get("weakness_task_id")
    assert weakness_task_id, "expected weakness_task_id on upload response"
    for _ in range(30):
        s = client.get(f"/api/tasks/{weakness_task_id}").json()
        if s["status"] in {"done", "error"}:
            break
        import time; time.sleep(0.2)
    assert s["status"] == "done", s

    # 5. Generate scenarios (background task)
    r = client.post(f"/api/assessments/{aid}/scenarios/generate")
    assert r.status_code == 200
    task_id = r.json()["task_id"]
    # Poll until done
    for _ in range(20):
        s = client.get(f"/api/tasks/{task_id}").json()
        if s["status"] in {"done", "error"}:
            break
        import time; time.sleep(0.2)
    assert s["status"] == "done", s

    # 6. List scenarios
    r = client.get(f"/api/assessments/{aid}/scenarios")
    assert r.status_code == 200
    scenarios = r.json()
    assert len(scenarios) == 1
    assert scenarios[0]["code"] == "DATA_LEAK"

    # 7. Run gap analysis
    r = client.post(f"/api/assessments/{aid}/gap-analysis/run")
    assert r.status_code == 200
    task_id = r.json()["task_id"]
    for _ in range(40):
        s = client.get(f"/api/tasks/{task_id}").json()
        if s["status"] in {"done", "error"}:
            break
        import time; time.sleep(0.2)
    assert s["status"] == "done", s

    # 8. Weakness synthesis
    r = client.post(f"/api/assessments/{aid}/weaknesses/synthesize")
    assert r.status_code == 200
    task_id = r.json()["task_id"]
    for _ in range(20):
        s = client.get(f"/api/tasks/{task_id}").json()
        if s["status"] in {"done", "error"}:
            break
        import time; time.sleep(0.2)
    assert s["status"] == "done", s

    # 9. Recalculate
    r = client.post(f"/api/assessments/{aid}/recalculate")
    assert r.status_code == 200
    payload = r.json()
    assert payload["aggregate"]["band"] in {"Low", "Moderate", "High", "VeryHigh"}
    assert payload["scenarios"][0]["band"] in {"Low", "Moderate"}  # full coverage strong/adequate

    # 10. Edit a control assessment, recalc, expect a change in coverage_index
    sc = scenarios[0]
    r = client.get(f"/api/assessments/{aid}/scenarios")
    sc = r.json()[0]
    ec_id = sc["expected_controls"][0]["id"]
    ca_id = sc["expected_controls"][0]["assessment"]["id"]
    r = client.patch(f"/api/control-assessments/{ca_id}",
                     json={"coverage": "none", "effectiveness": "weak"})
    assert r.status_code == 200
    r = client.post(f"/api/assessments/{aid}/recalculate")
    assert r.status_code == 200
    new_idx = r.json()["scenarios"][0]["coverage_index"]
    assert new_idx < payload["scenarios"][0]["coverage_index"]

    # 11. Report
    r = client.get(f"/api/assessments/{aid}/report")
    assert r.status_code == 200
    rep = r.json()
    assert rep["assessment"]["vendor_name"] == "Acme Billing"
    assert rep["aggregate"]["band"] in {"Low", "Moderate", "High", "VeryHigh"}
