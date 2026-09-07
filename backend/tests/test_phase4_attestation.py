"""Accuracy program Phase 4: attestation profile + deterministic checks."""

from __future__ import annotations

from datetime import date

import pytest

from app.ai.agents import attestation
from app.attestation_checks import check_profile, check_required_attestations
from app.db import SessionLocal
from app.models import Assessment, Chunk, Document, Weakness
from app.schemas.attestation import AttestationProfileOut
from app.schemas.standards import StandardsProfile

from .conftest import advance_workflow

AS_OF = date(2026, 5, 1)
EMPTY = StandardsProfile()


def _soc(**kw):
    base = {
        "doc_type": "soc2_type2",
        "period_start": {"value": "2025-01-01", "quote": "1 January 2025"},
        "period_end": {"value": "2025-06-30", "quote": "to 30 June 2025"},
    }
    base.update(kw)
    return AttestationProfileOut.model_validate(base)


def test_short_first_examination_is_one_medium_weakness_with_all_quotes():
    p = _soc(first_examination={"value": True, "quote": "this is the first examination"})
    (f,) = [x for x in check_profile(p, AS_OF, EMPTY, doc_label="soc.pdf") if x.code == "soc_short_first_examination"]
    assert f.kind == "weakness" and f.severity == "medium"
    assert "~6 months" in f.description and "FIRST Type 2" in f.description
    assert f.quotes == ["1 January 2025", "to 30 June 2025", "this is the first examination"]


def test_short_period_without_first_flag_and_full_period_clean():
    (f,) = [x for x in check_profile(_soc(), AS_OF, EMPTY, doc_label="d") if x.code == "soc_short_period"]
    assert f.severity == "medium"
    full = _soc(period_start={"value": "2024-07-01", "quote": "from 1 July 2024"}, period_end={"value": "2025-06-30", "quote": "to 30 June 2025"})
    assert [x.code for x in check_profile(full, AS_OF, EMPTY, doc_label="d")] == []


def test_staleness_uses_standards_and_bridge_letter_downgrades():
    old = _soc(period_start={"value": "2024-01-01", "quote": "from 1 January 2024"}, period_end={"value": "2024-12-31", "quote": "to 31 December 2024"})
    strict = StandardsProfile(attestation_max_age_months=12)
    (f,) = [x for x in check_profile(old, AS_OF, strict, doc_label="d") if x.code == "attestation_stale"]
    assert f.kind == "weakness" and f.severity == "medium" and "16 months" in f.description
    bridged = AttestationProfileOut.model_validate(
        {**old.model_dump(exclude_none=True), "bridge_letter": {"value": True, "quote": "bridge letter dated"}})
    (f2,) = [x for x in check_profile(bridged, AS_OF, strict, doc_label="d") if x.code == "attestation_stale"]
    assert f2.kind == "evidence_note" and "bridge letter is referenced" in f2.description
    # default 12 months: 10-month-old period end is NOT stale (orbitclear case)
    assert not any(x.code == "attestation_stale" for x in check_profile(_soc(), AS_OF, EMPTY, doc_label="d"))


def test_iso_expired_and_pentest_stale():
    iso = AttestationProfileOut.model_validate({
        "doc_type": "iso27001_certificate",
        "cert_expiry_date": {"value": "2026-01-15", "quote": "valid until 15 January 2026"}})
    (f,) = check_profile(iso, AS_OF, EMPTY, doc_label="iso.pdf")
    assert f.code == "iso_certificate_expired" and f.severity == "high"
    pt = AttestationProfileOut.model_validate({
        "doc_type": "pentest", "test_end_date": {"value": "2024-11-15", "quote": "testing concluded 15 Nov 2024"}})
    (f,) = check_profile(pt, AS_OF, EMPTY, doc_label="pt.pdf")
    assert f.code == "pentest_stale" and f.severity == "medium"


def test_required_attestations_matching():
    st = StandardsProfile(required_attestations=["SOC 2 Type 2", "ISO/IEC 27001", "annual penetration test"])
    out = check_required_attestations({"soc.pdf": _soc()}, st)
    assert sorted(f.code for f in out) == [
        "required_attestation_missing:annual penetration test",
        "required_attestation_missing:iso/iec 27001",
    ]
    assert all(f.kind == "weakness" and f.severity == "high" for f in out)


def test_uploaded_assurance_doc_counts_as_supplied_without_profile():
    """A pen test whose profile extraction failed is still a supplied pen
    test: the requirement check must not report it as missing (run 23 on
    veltrix produced exactly that false high-severity weakness)."""
    from app.attestation_checks import missing_profile_note

    st = StandardsProfile(required_attestations=["SOC 2 Type 2", "annual penetration test"])
    out = check_required_attestations({"soc.pdf": _soc()}, st, supplied_kinds={"soc", "pentest"})
    assert out == []
    # Still missing when neither a profile nor an upload evidences it.
    out = check_required_attestations({}, st, supplied_kinds={"pentest"})
    assert [f.code for f in out] == ["required_attestation_missing:soc 2 type 2"]
    assert "pentest (upload kind)" in out[0].description
    note = missing_profile_note("pt.pdf", "pentest", "garbled output twice: ValidationError")
    assert note.kind == "evidence_note" and note.code == "attestation_profile_missing"
    assert "still counts as supplied" in note.description and "garbled output twice" in note.description


def test_sibling_quote_encoding_is_folded_but_quote_still_required():
    """`report_date` + `report_date_quote` (the doc_type convention generalised)
    is the same information as {value, quote}; a scalar with no sibling quote
    still fails — the quote floor is unchanged."""
    p = AttestationProfileOut.model_validate({
        "doc_type": "pentest", "doc_type_quote": "Penetration Test Report",
        "report_date": "2025-11-28", "report_date_quote": "Report date: 28 November 2025",
        "scope": "Web application", "scope_quote": "In scope: the web application",
        "bridge_letter": False,
        "methodology": "OWASP", "methodology_quote": "ignored extra field",
    })
    assert p.report_date.value == "2025-11-28" and p.report_date.quote.startswith("Report date")
    assert p.scope.value == "Web application"
    assert p.bridge_letter.value is False  # False needs no quote
    with pytest.raises(Exception):
        AttestationProfileOut.model_validate({"doc_type": "pentest", "report_date": "2025-11-28"})
    with pytest.raises(Exception):
        AttestationProfileOut.model_validate({"doc_type": "pentest", "report_date": "2025-11-28",
                                              "report_date_quote": ""})


def test_profile_field_without_quote_is_rejected():
    with pytest.raises(Exception):
        AttestationProfileOut.model_validate({"doc_type": "soc2_type2", "period_end": {"value": "2025-06-30", "quote": ""}})
    with pytest.raises(Exception):
        AttestationProfileOut.model_validate({"doc_type": "soc2_type2", "period_end": {"value": "not-a-date", "quote": "q"}})


@pytest.mark.asyncio
async def test_extract_profile_persists_and_apply_checks_creates_reviewed_rows(fresh_db, fake_client):
    fake_client.push_json({
        "doc_type": "soc2_type2", "doc_type_quote": "SOC 2 Type 2",
        "period_start": {"value": "2025-01-01", "quote": "1 January 2025"},
        "period_end": {"value": "2025-06-30", "quote": "30 June 2025"},
        "first_examination": {"value": True, "quote": "first examination of the system"},
        "opinion": {"value": "qualified", "quote": "except for the matters described"},
    })
    with SessionLocal() as db:
        a = Assessment(vendor_name="Acme", as_of_date="2026-05-01")
        db.add(a)
        db.flush()
        doc = Document(assessment_id=a.id, kind="soc", filename="soc.pdf", mime="x", sha256="s", size_bytes=1)
        db.add(doc)
        db.flush()
        db.add(Chunk(document_id=doc.id, section_path="I", ord=0,
                     text="This report covers the period 1 January 2025 to 30 June 2025 and is the first examination of the system. Except for the matters described above."))
        db.commit()
        out = await attestation.extract_profile(db, doc.id, client=fake_client)
        assert out is not None and fake_client.calls[0]["model"]  # fast profile call happened
        db.refresh(doc)
        assert doc.attestation_profile["period_end"]["value"] == "2025-06-30"

        counts = attestation.apply_checks(db, a.id)
        assert counts == {"weakness": 1, "evidence_note": 1}
        rows = db.query(Weakness).filter(Weakness.assessment_id == a.id).order_by(Weakness.id).all()
        w = next(r for r in rows if r.status == "confirmed")
        assert w.origin == "attestation_check" and w.kind_signal == "attestation_check"
        assert "~6 months" in w.description and "FIRST Type 2" in w.description
        assert w.source_chunk_id is not None  # quotes bound to the chunk
        assert all(r["chunk_id"] for r in w.evidence_refs)
        assert w.review["stage"] == "attestation_check" and "2026-05-01" in w.review["reason"]
        note = next(r for r in rows if r.status == "evidence_note")
        assert note.review["reason"].startswith("deterministic attestation check")

        # idempotent re-run replaces rows (same count, no duplicates)
        counts2 = attestation.apply_checks(db, a.id)
        assert counts2 == counts
        assert db.query(Weakness).filter(Weakness.assessment_id == a.id).count() == 2


def test_apply_checks_reports_missing_profile_as_note_not_missing_attestation(fresh_db):
    """Run 23 (veltrix): the pen test profile failed on a fast-model formatting
    slip and the required-attestation check then reported the pen test as not
    supplied — a false high. Now: no such weakness; one evidence note that
    says the document was supplied but not machine-checked."""
    from app.attestation_checks import missing_profile_note  # noqa: F401  (documented pairing)

    with SessionLocal() as db:
        a = Assessment(vendor_name="Acme", as_of_date="2026-09-02",
                       standards_profile={"required_attestations": ["Independent penetration test report, annually"]})
        db.add(a)
        db.flush()
        doc = Document(assessment_id=a.id, kind="pentest", filename="pt.pdf", mime="x", sha256="p", size_bytes=1,
                       attestation_profile=None, attestation_profile_error="garbled output twice: ValidationError …")
        db.add(doc)
        db.flush()
        db.add(Chunk(document_id=doc.id, section_path="1", ord=0, text="Penetration test report, 28 November 2025."))
        db.commit()

        counts = attestation.apply_checks(db, a.id)
        assert counts == {"weakness": 0, "evidence_note": 1}
        rows = db.query(Weakness).filter(Weakness.assessment_id == a.id).all()
        assert not any("required_attestation_missing" in (r.dedupe_key or "") for r in rows)
        (note,) = rows
        assert note.status == "evidence_note" and note.source_document_id == doc.id
        assert "still counts as supplied" in note.description
        assert "garbled output twice" in note.description

        # Once a profile exists the note disappears and the real checks run.
        doc.attestation_profile = {"doc_type": "pentest",
                                   "test_end_date": {"value": "2025-11-28", "quote": "28 November 2025"}}
        doc.attestation_profile_error = None
        db.commit()
        counts = attestation.apply_checks(db, a.id)
        assert counts["evidence_note"] == 0
        assert not any("attestation_profile_missing" in (r.dedupe_key or "") for r in
                       db.query(Weakness).filter(Weakness.assessment_id == a.id).all())


@pytest.mark.asyncio
async def test_extract_profile_skips_non_attestation_kinds(fresh_db, fake_client):
    with SessionLocal() as db:
        a = Assessment(vendor_name="Acme")
        db.add(a)
        db.flush()
        doc = Document(assessment_id=a.id, kind="policy", filename="p.pdf", mime="x", sha256="s", size_bytes=1)
        db.add(doc)
        db.commit()
        assert await attestation.extract_profile(db, doc.id, client=fake_client) is None
        assert fake_client.calls == []


def test_doc_type_normalisation():
    for raw, want in [("SOC 2 Type II", "soc2_type2"), ("soc 2 type 2 report", "soc2_type2"),
                      ("SOC2-Type2", "soc2_type2"), ("ISO/IEC 27001 Certificate", "iso27001_certificate"),
                      ("ISO 27001 surveillance audit", "iso27001_audit"), ("Penetration Test Attestation", "pentest"),
                      ("bridge letter", "other"), (None, "other")]:
        assert AttestationProfileOut.model_validate({"doc_type": raw}).doc_type == want, raw


@pytest.mark.asyncio
async def test_upload_job_survives_profile_failure(fresh_db, fake_client, monkeypatch):
    """A garbled attestation-profile response must not fail document extraction."""
    from fastapi.testclient import TestClient
    from app import main as main_mod
    from app.ai import router as router_mod
    monkeypatch.setattr(router_mod, "OpenRouterClient", lambda *a, **kw: fake_client)
    import io
    import pymupdf
    with TestClient(main_mod.app) as client:
        r = client.post("/api/assessments", json={"vendor_name": "Acme"})
        aid = r.json()["id"]
        advance_workflow(aid, "scenarios", with_document=False)  # upload is gated on scenarios
        fake_client.push_json({"weaknesses": []})
        fake_client.push_json({"doc_type": ["not", "a", "string"], "period_end": "garbage"})  # invalid twice
        fake_client.push_json({"doc_type": ["still"], "period_end": "garbage"})
        pdf = pymupdf.open()
        page = pdf.new_page()
        page.insert_text((72, 72), "SOC 2 Type 2 report for the period 1 Jan 2025 to 30 Jun 2025.")
        buf = pdf.tobytes()
        r = client.post(f"/api/assessments/{aid}/documents", data={"kind": "soc"},
                        files={"file": ("soc.pdf", io.BytesIO(buf), "application/pdf")})
        tid = r.json()["weakness_task_id"]
        st = client.get(f"/api/tasks/{tid}").json()
        assert st["status"] == "done", st
        assert "attestation profile failed" in st["detail"]
        doc_id = r.json()["id"]
        # The failure is persisted on the row, not only in the transient task detail.
        d = client.get(f"/api/assessments/{aid}/documents").json()
        (row,) = [x for x in d if x["id"] == doc_id]
        assert row["attestation_profile"] is None
        assert row["attestation_profile_error"], row
        # re-run endpoint recovers once the model answers sanely
        fake_client.push_json({"doc_type": "SOC 2 Type II",
                               "period_end": {"value": "2025-06-30", "quote": "to 30 Jun 2025"}})
        r = client.post(f"/api/documents/{doc_id}/attestation-profile")
        assert r.status_code == 200
        assert r.json()["attestation_profile"]["doc_type"] == "soc2_type2"
        assert r.json()["attestation_profile_error"] is None
