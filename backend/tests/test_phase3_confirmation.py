"""Accuracy program Phase 3 (R3 confirmation + R4 thin merge)."""

from __future__ import annotations

import pytest

from app.ai.agents import confirmation, cross_correlation, document_weaknesses
from app.db import SessionLocal
from app.models import Assessment, Chunk, Document, ModelCall, Weakness


def _fixture(db):
    a = Assessment(vendor_name="Acme")
    db.add(a)
    db.flush()
    sig = Document(assessment_id=a.id, kind="questionnaire", filename="sig.xlsx", mime="x", sha256="a", size_bytes=1)
    pol = Document(assessment_id=a.id, kind="policy", filename="bcdr.pdf", mime="x", sha256="b", size_bytes=1)
    db.add_all([sig, pol])
    db.flush()
    c1 = Chunk(document_id=sig.id, section_path="Sheet 'G' (rows 1-2)", ord=0,
               text="BC-01 Is DR tested annually? | Response: See comment | Comment: Yes, full cut-over test 14-15 Feb 2026.")
    c2 = Chunk(document_id=pol.id, section_path="6.4", ord=0,
               text="Immutable archive copies are replicated to Amazon S3 Glacier Deep Archive in AWS us-east-2 (Ohio).")
    c3 = Chunk(document_id=pol.id, section_path="7", ord=1, text="The BC/DR policy does not define RTO for the archive.")
    db.add_all([c1, c2, c3])
    db.flush()
    rows = [
        Weakness(assessment_id=a.id, source_document_id=sig.id, source_chunk_id=c1.id, severity="medium",
                 description="Vendor did not confirm annual DR testing (answer: See comment).",
                 quote="Response: See comment", kind_signal="questionnaire_negative", status="candidate", dedupe_key="k1"),
        Weakness(assessment_id=a.id, source_document_id=pol.id, source_chunk_id=c2.id, severity="high",
                 description="Regulatory archive replicated to a US region despite EEA-only commitment.",
                 quote="replicated to Amazon S3 Glacier Deep Archive in AWS us-east-2", kind_signal="policy_gap",
                 status="candidate", dedupe_key="k2"),
        Weakness(assessment_id=a.id, source_document_id=pol.id, source_chunk_id=c2.id, severity="medium",
                 description="Archive copies stored in Ohio (us-east-2), outside the EEA.",
                 quote="AWS us-east-2 (Ohio)", kind_signal="policy_gap", status="candidate", dedupe_key="k3"),
        Weakness(assessment_id=a.id, source_document_id=pol.id, source_chunk_id=c3.id, severity="low",
                 description="Policy does not name an owner for the archive.", quote="does not define RTO",
                 kind_signal="policy_gap", status="candidate", dedupe_key="k4"),
    ]
    db.add_all(rows)
    db.commit()
    db.refresh(a)
    return a, sig, pol, [r.id for r in rows]


@pytest.mark.asyncio
async def test_confirmation_decides_every_candidate_and_logs_reasons(fresh_db, fake_client):
    with SessionLocal() as db:
        a, sig, pol, (w1, w2, w3, w4) = _fixture(db)
        # one confirmation call per source document (order: sig first, then pol)
        fake_client.push_json({"decisions": [
            {"id": w1, "decision": "dropped", "confidence": "high",
             "reason": "The comment in the same row confirms an annual full cut-over test (Feb 2026)."}]})
        fake_client.push_json({"decisions": [
            {"id": w2, "decision": "confirmed", "confidence": "high", "reason": "BC/DR §6.4 vs SIG DS-02 EEA-only claim."},
            {"id": w3, "decision": "confirmed", "confidence": "medium", "reason": "Same fact as w2 from the same section."},
            # w4 omitted on purpose → kept + flagged
        ]})
        fake_client.push_json({"groups": [
            {"primary_id": w2, "member_ids": [w3],
             "description": "Immutable regulatory archive copies are replicated to AWS us-east-2 (Ohio), outside the EEA, contrary to the EEA-only commitment.",
             "reason": "Both rows state the same archive-residency deficiency."}]})
        counts = await confirmation.review(db, a.id, client=fake_client)
        assert counts == {"confirmed": 2, "evidence_note": 0, "dropped": 1, "unreviewed_kept": 1, "merged": 1}

        rows = {w.id: w for w in db.query(Weakness).filter(Weakness.assessment_id == a.id).all()}
        assert rows[w1].status == "dropped" and "comment" in rows[w1].review["reason"]
        assert rows[w2].status == "confirmed" and rows[w2].review["confidence"] == "high"
        assert rows[w3].status == "merged" and rows[w3].review["merged_into"] == w2
        assert rows[w4].status == "confirmed" and rows[w4].review["unreviewed"] is True
        # primary carries every member quote as an evidence ref, description consolidated
        assert [r["quote"] for r in rows[w2].evidence_refs] == [
            "replicated to Amazon S3 Glacier Deep Archive in AWS us-east-2", "AWS us-east-2 (Ohio)"]
        assert rows[w2].review["members"] == [w3]
        assert rows[w2].description.startswith("Immutable regulatory archive")
        # every non-confirmed row has a logged reason (floor)
        for w in rows.values():
            if w.status != "confirmed":
                assert w.review and (w.review.get("reason") or w.review.get("merge_reason"))
        # reported set = confirmed only
        db.refresh(a)
        assert sorted(w.id for w in a.weaknesses) == sorted([w2, w4])
        assert len(a.all_weaknesses) == 4
        purposes = [m.purpose for m in db.query(ModelCall).order_by(ModelCall.id).all()]
        assert purposes == ["weakness_confirmation", "weakness_confirmation", "weakness_merge"]
        user = fake_client.calls[0]["messages"][1]["content"]
        assert "# Evidence bundle — 2 document(s)" in user and f"Candidate weaknesses extracted from \"sig.xlsx\"" in user


@pytest.mark.asyncio
async def test_cross_correlation_reviews_before_mapping(fresh_db, fake_client):
    """Candidates never reach correlation or scoring unreviewed."""
    with SessionLocal() as db:
        a, sig, pol, (w1, w2, w3, w4) = _fixture(db)
        assert a.weaknesses == []  # candidates are not reported
        fake_client.push_json({"decisions": [{"id": w1, "decision": "evidence_note", "confidence": "high",
                                              "reason": "Comment confirms the test; useful context only."}]})
        fake_client.push_json({"decisions": [
            {"id": i, "decision": "dropped", "confidence": "high", "reason": "test: not a deficiency here"}
            for i in (w2, w3, w4)]})
        stats = await cross_correlation.run(db, a.id, client=fake_client)
        # nothing confirmed → nothing to correlate, and no merge call for < 2 rows
        assert stats["mapped"] == 0 and stats["review_dropped"] == 3 and stats["review_evidence_note"] == 1
        assert len(fake_client.calls) == 2
        db.refresh(a)
        assert a.weaknesses == [] and len(a.all_weaknesses) == 4


@pytest.mark.asyncio
async def test_extraction_creates_candidates_not_reported_rows(fresh_db, fake_client):
    fake_client.push_json({"weaknesses": [{
        "severity": "medium", "description": "MFA is not enforced for contractors.", "quote": "contractors exempt from MFA",
        "section_path": "1", "page": 1, "kind_signal": "policy_gap", "suggested_control_codes": ["IAM.MFA"]}]})
    with SessionLocal() as db:
        a = Assessment(vendor_name="Acme")
        db.add(a)
        db.flush()
        doc = Document(assessment_id=a.id, kind="policy", filename="p.pdf", mime="x", sha256="x", size_bytes=1)
        db.add(doc)
        db.flush()
        db.add(Chunk(document_id=doc.id, section_path="1", ord=0, text="Contractors exempt from MFA."))
        db.commit()
        assert await document_weaknesses.extract(db, doc.id, client=fake_client) == 1
        db.refresh(a)
        assert a.weaknesses == [] and a.all_weaknesses[0].status == "candidate"


def test_legacy_rows_and_user_rows_default_to_confirmed(fresh_db):
    with SessionLocal() as db:
        a = Assessment(vendor_name="Acme")
        db.add(a)
        db.flush()
        db.add(Weakness(assessment_id=a.id, severity="low", description="legacy row", quote="", dedupe_key="l"))
        db.commit()
        db.refresh(a)
        assert [w.status for w in a.weaknesses] == ["confirmed"]


@pytest.mark.asyncio
async def test_confirmation_truncation_splits_and_failure_keeps_candidates(fresh_db, fake_client):
    with SessionLocal() as db:
        a, sig, pol, (w1, w2, w3, w4) = _fixture(db)
        # sig doc (1 candidate): truncated once at 8192 → enlarged retry also truncated → single row → kept+flagged
        fake_client.push_truncated("")
        fake_client.push_truncated("")
        # pol doc (3 candidates): truncated twice → split into [w2] + [w3, w4]
        fake_client.push_truncated("")
        fake_client.push_truncated("")
        fake_client.push_json({"decisions": [
            {"id": w2, "decision": "confirmed", "confidence": "high", "reason": "real archive residency deficiency"}]})
        fake_client.push_json({"decisions": [
            {"id": w3, "decision": "dropped", "confidence": "high", "reason": "restates candidate w2 verbatim"},
            {"id": w4, "decision": "evidence_note", "confidence": "medium", "reason": "owner naming is context only"}]})
        fake_client.push_json({"groups": []})  # merge pass over the two confirmed rows
        counts = await confirmation.review(db, a.id, client=fake_client)
        assert counts["confirmed"] == 1 and counts["dropped"] == 1 and counts["evidence_note"] == 1
        assert counts["unreviewed_kept"] == 1  # the sig candidate survived the failed call
        rows = {w.id: w for w in db.query(Weakness).filter(Weakness.assessment_id == a.id).all()}
        assert rows[w1].status == "confirmed" and rows[w1].review["unreviewed"] is True
        assert "confirmation call failed" in rows[w1].review["reason"]
        assert rows[w2].status == "confirmed" and rows[w3].status == "dropped"


@pytest.mark.asyncio
async def test_merge_splits_on_truncation(fresh_db, fake_client):
    with SessionLocal() as db:
        a, sig, pol, (w1, w2, w3, w4) = _fixture(db)
        # confirm all four (one call per source doc)
        fake_client.push_json({"decisions": [
            {"id": w1, "decision": "confirmed", "confidence": "high", "reason": "keep for merge test"}]})
        fake_client.push_json({"decisions": [
            {"id": i, "decision": "confirmed", "confidence": "high", "reason": "keep for merge test"}
            for i in (w2, w3, w4)]})
        # merge over 4 rows truncates → split into two halves of 2
        fake_client.push_truncated("")
        fake_client.push_truncated("")
        fake_client.push_json({"groups": []})
        fake_client.push_json({"groups": [
            {"primary_id": w3, "member_ids": [w4], "description": "same archive deficiency stated twice over",
             "reason": "same section restated"}]})
        counts = await confirmation.review(db, a.id, client=fake_client)
        assert counts["merged"] == 1
        rows = {w.id: w for w in db.query(Weakness).filter(Weakness.assessment_id == a.id).all()}
        assert rows[w4].status == "merged" and rows[w4].review["merged_into"] == w3
        assert rows[w1].status == "confirmed" and rows[w2].status == "confirmed"
