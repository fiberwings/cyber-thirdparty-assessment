"""Accuracy program Phase 2 (R1): whole-bundle gap analysis."""

from __future__ import annotations

import pytest

from app.ai.agents import gap_analysis
from app.db import SessionLocal
from app.models import Assessment, Chunk, Document, ExpectedControl, ModelCall, Scenario, Weakness

MFA_OUT = {
    "control_code": "IAM.MFA", "coverage": "full", "effectiveness": "weak",
    "citations": [{"document_id": None, "section_path": "CC6.1",
                   "quote": "All administrative users must authenticate with MFA."}],
    "rationale": "Documented, but the SOC 2 exception shows 3 of 40 admins without MFA.",
    "meta_flags": [],
    "contradictions": [{
        "severity": "high",
        "description": "Questionnaire claims MFA on 100% of accounts with no exceptions; SOC 2 AC-04 exception shows 3 of 40 privileged accounts without MFA.",
        "claims": [
            {"document_id": None, "section_path": "AC-03", "quote": "MFA is enforced on 100% of accounts with no exceptions."},
            {"document_id": None, "section_path": "CC6.1", "quote": "3 of 40 sampled privileged accounts operated without MFA enrolment."},
        ],
    }],
    "proposed_queries": [],
}
SIEM_OUT = {
    "control_code": "LOG.SIEM", "coverage": "none", "effectiveness": "unknown",
    "citations": [], "rationale": "The bundle is silent on centralised logging.",
    "meta_flags": ["insufficient_info"], "contradictions": [], "proposed_queries": [],
}


def _fixture(db):
    """Two scenarios sharing IAM.MFA; one also expects LOG.SIEM. Two documents."""
    a = Assessment(vendor_name="Acme", as_of_date="2026-05-01")
    db.add(a)
    db.flush()
    s1 = Scenario(assessment_id=a.id, code="DATA_LEAK", name="Leak", description="x",
                  inherent_impact=3, inherent_likelihood=3)
    s2 = Scenario(assessment_id=a.id, code="IDP_BYPASS", name="IdP bypass", description="y",
                  inherent_impact=3, inherent_likelihood=2)
    db.add_all([s1, s2])
    db.flush()
    db.add_all([
        ExpectedControl(scenario_id=s1.id, code="IAM.MFA", name="MFA", rationale="leak via creds"),
        ExpectedControl(scenario_id=s1.id, code="LOG.SIEM", name="SIEM"),
        ExpectedControl(scenario_id=s2.id, code="IAM.MFA", name="Multi-factor auth", rationale="idp"),
    ])
    sig = Document(assessment_id=a.id, kind="questionnaire", filename="sig.xlsx",
                   mime="x", sha256="a", size_bytes=1)
    soc = Document(assessment_id=a.id, kind="soc", filename="soc.pdf",
                   mime="x", sha256="b", size_bytes=1)
    db.add_all([sig, soc])
    db.flush()
    db.add(Chunk(document_id=sig.id, section_path="AC-03", ord=0,
                 text="AC-03 MFA is enforced on 100% of accounts with no exceptions."))
    db.add(Chunk(document_id=soc.id, page=7, section_path="CC6.1", ord=0,
                 text="All administrative users must authenticate with MFA. Exception: 3 of 40 sampled "
                      "privileged accounts operated without MFA enrolment."))
    db.commit()
    for o in (a, s1, s2):
        db.refresh(o)
    return a, s1, s2, sig.id, soc.id


def _with_docs(out, sig_id, soc_id):
    o = {**out, "citations": [{**c, "document_id": soc_id} for c in out["citations"]]}
    o["contradictions"] = [
        {**k, "claims": [{**cl, "document_id": sig_id if cl["section_path"] == "AC-03" else soc_id}
                         for cl in k["claims"]]}
        for k in out["contradictions"]
    ]
    return o


@pytest.mark.asyncio
async def test_bundle_mode_one_call_consistent_verdicts_single_contradiction(fresh_db, fake_client):
    with SessionLocal() as db:
        a, s1, s2, sig_id, soc_id = _fixture(db)
        fake_client.push_json({"controls": [_with_docs(MFA_OUT, sig_id, soc_id), SIEM_OUT]})
        result = await gap_analysis.run_full(db, a, client=fake_client)
        assert result.total == 3 and not result.failed

        # one whole-bundle call carrying every document and both codes
        assert len(fake_client.calls) == 1
        user = fake_client.calls[0]["messages"][1]["content"]
        assert "# Evidence bundle — 2 document(s)" in user
        assert "sig.xlsx" in user and "soc.pdf" in user
        assert "# Controls to assess (2): IAM.MFA, LOG.SIEM" in user
        assert "protects scenarios: DATA_LEAK — Leak; IDP_BYPASS — IdP bypass" in user
        assert "# Analysis date: 2026-05-01" in user
        purposes = [m.purpose for m in db.query(ModelCall).all()]
        assert purposes == ["gap_analysis_bundle"]

        db.expire_all()
        db.refresh(a)
        # same code → identical verdict in both scenarios, citation bound to the SOC chunk
        mfa = [ec for s in a.scenarios for ec in s.expected_controls if ec.code == "IAM.MFA"]
        assert len(mfa) == 2
        assert {(ec.assessment.coverage, ec.assessment.effectiveness) for ec in mfa} == {("full", "weak")}
        for ec in mfa:
            assert ec.assessment.last_error is None
            assert [ev.chunk.section_path for ev in ec.assessment.evidence if ev.polarity == "supports"] == ["CC6.1"]
            assert ec.assessment.unresolved_citations == []
        siem = [ec for s in a.scenarios for ec in s.expected_controls if ec.code == "LOG.SIEM"][0]
        assert siem.assessment.coverage == "none"
        # the contradiction is one weakness row claimed by both scenarios
        rows = db.query(Weakness).filter(Weakness.assessment_id == a.id).all()
        assert len(rows) == 1
        w = rows[0]
        assert w.kind_signal == "cross_doc_conflict" and w.mapped_control_codes == ["IAM.MFA"]
        assert sorted(w.origin_refs) == ["DATA_LEAK/IAM.MFA", "IDP_BYPASS/IAM.MFA"]
        assert all(r["chunk_id"] for r in w.evidence_refs)
        assert len(db.query(gap_analysis.MetaIssue).filter_by(assessment_id=a.id).all()) == 1


@pytest.mark.asyncio
async def test_bundle_mode_fill_call_for_omitted_code(fresh_db, fake_client):
    with SessionLocal() as db:
        a, s1, s2, sig_id, soc_id = _fixture(db)
        fake_client.push_json({"controls": [_with_docs(MFA_OUT, sig_id, soc_id)]})  # LOG.SIEM omitted
        fake_client.push_json({"controls": [SIEM_OUT]})
        result = await gap_analysis.run_full(db, a, client=fake_client)
        assert not result.failed
        purposes = [m.purpose for m in db.query(ModelCall).order_by(ModelCall.id).all()]
        assert purposes == ["gap_analysis_bundle", "gap_analysis_bundle_fill"]
        assert "# Controls to assess (1): LOG.SIEM" in fake_client.calls[1]["messages"][1]["content"]


@pytest.mark.asyncio
async def test_bundle_mode_batch_failure_is_per_control_and_resumable(fresh_db, fake_client):
    with SessionLocal() as db:
        a, s1, s2, sig_id, soc_id = _fixture(db)
        # first batch call fails (no canned response) → all 3 targets failed, phase still returns
        with pytest.raises(RuntimeError, match="0/3 controls assessed"):
            await gap_analysis.run_full(db, a, client=fake_client)
        db.expire_all()
        db.refresh(a)
        assert len(gap_analysis.failed_targets(a)) == 3
        # resume: only_failed re-batches the failed codes; success clears them
        fake_client.push_json({"controls": [_with_docs(MFA_OUT, sig_id, soc_id), SIEM_OUT]})
        result = await gap_analysis.run_full(db, a, client=fake_client, only_failed=True)
        assert result.total == 3 and not result.failed
        db.expire_all()
        db.refresh(a)
        assert gap_analysis.failed_targets(a) == []


@pytest.mark.asyncio
async def test_oversized_bundle_falls_back_to_retrieval_mode(fresh_db, fake_client, monkeypatch):
    monkeypatch.setattr(gap_analysis, "WHOLE_BUNDLE_MAX_TOKENS", 0)
    with SessionLocal() as db:
        a, s1, s2, sig_id, soc_id = _fixture(db)
        per_control = {k: v for k, v in _with_docs(MFA_OUT, sig_id, soc_id).items()}
        for _ in range(3):  # 3 targets → 3 per-control calls (single-control shape)
            fake_client.push_json({**per_control, "contradictions": []})
        fake_client.push_json({**SIEM_OUT})
        await gap_analysis.run_full(db, a, client=fake_client)
        purposes = {m.purpose for m in db.query(ModelCall).all()}
        assert purposes <= {"gap_analysis_control", "gap_analysis_control_r2"}
        assert "gap_analysis_bundle" not in purposes


def test_batch_codes_keeps_families_together():
    codes = ["IAM.MFA", "IAM.RBAC", "IAM.PRIV", "NET.FW", "NET.SEG", "LOG.SIEM", "ENC.REST", "ENC.TRANSIT",
             "ENC.KEYS", "BCP.DR"]
    batches = gap_analysis.batch_codes(codes, size=4)
    assert all(len(b) <= 4 for b in batches)
    assert sorted(c for b in batches for c in b) == sorted(codes)
    fam_of = {c: c.split(".")[0] for c in codes}
    # a family of ≤ size codes is never split across batches
    for fam in {"IAM", "NET", "ENC"}:
        holders = {i for i, b in enumerate(batches) if any(fam_of[c] == fam for c in b)}
        assert len(holders) == 1, (fam, batches)
    # oversized family is split into ≤ size pieces
    big = [f"X.{i}" for i in range(9)]
    assert [len(b) for b in gap_analysis.batch_codes(big, size=4)] == [4, 4, 1]


@pytest.mark.asyncio
async def test_bundle_truncation_splits_batch(fresh_db, fake_client):
    with SessionLocal() as db:
        a, s1, s2, sig_id, soc_id = _fixture(db)
        # batch call truncated on every ladder step (router exhausts the cap →
        # raises truncated) — one canned truncation per attempt.
        fake_client.push_truncated("")
        fake_client.push_truncated("")
        # halves: [IAM.MFA] then [LOG.SIEM]
        fake_client.push_json({"controls": [_with_docs(MFA_OUT, sig_id, soc_id)]})
        fake_client.push_json({"controls": [SIEM_OUT]})
        result = await gap_analysis.run_full(db, a, client=fake_client)
        assert not result.failed
        purposes = [m.purpose for m in db.query(ModelCall).order_by(ModelCall.id).all()]
        assert purposes == ["gap_analysis_bundle", "gap_analysis_bundle_split", "gap_analysis_bundle_split"]


@pytest.mark.asyncio
async def test_reported_contradictions_are_passed_to_later_batches(fresh_db, fake_client, monkeypatch):
    monkeypatch.setattr(gap_analysis, "BUNDLE_BATCH_SIZE", 1)
    monkeypatch.setattr(gap_analysis, "BUNDLE_CONCURRENCY", 1)
    with SessionLocal() as db:
        a, s1, s2, sig_id, soc_id = _fixture(db)
        fake_client.push_json({"controls": [_with_docs(MFA_OUT, sig_id, soc_id)]})
        fake_client.push_json({"controls": [SIEM_OUT]})
        await gap_analysis.run_full(db, a, client=fake_client)
    first, second = (c["messages"][1]["content"] for c in fake_client.calls)
    assert "already reported" not in first
    assert "Contradictions already reported under other controls" in second
    assert "[IAM.MFA] Questionnaire claims MFA on 100% of accounts" in second


def test_quote_in_chunk_tolerates_abbreviated_lists():
    chunk = ("Tier 1 sub-processors as at the date of this Policy are: 1 Amazon Web Services, Inc. "
             "(hosting) 2 Okta, Inc. (identity) 3 Datadog EU S.à r.l. (monitoring)")
    assert gap_analysis._quote_in_chunk(
        "Tier 1 sub-processors as at the date of this Policy are: 1 Amazon Web Services, Inc. ... 2 Okta, Inc. ... 3 Datadog EU S.à r.l.", chunk)
    # fragments out of order, or absent → no match
    assert not gap_analysis._quote_in_chunk("3 Datadog EU S.à r.l. ... 1 Amazon Web Services, Inc.", chunk)
    assert not gap_analysis._quote_in_chunk("Tier 1 sub-processors as at ... Nexus Identity Services LLC", chunk)
    # short fragments are not trusted
    assert not gap_analysis._quote_in_chunk("Tier 1 ... Okta", chunk)


def test_quote_binding_ignores_markdown_and_typographic_punctuation():
    chunk = ("stored in AWS KMS (FIPS 140-2 Level 3 HSM). **Data encryption keys are rotated every 3 years**, "
             "consistent with industry practice and our risk assessment of the data sensitivity.")
    assert gap_analysis._quote_in_chunk(
        "Data encryption keys are rotated every 3 years, consistent with industry practice", chunk)
    table = "Carve-out\n\nNexus Identity Services LLC\nStep-up multi-factor authentication factor issuance\nand verification"
    assert gap_analysis._quote_in_chunk(
        "Nexus Identity Services LLC — Step-up multi-factor authentication factor issuance and verification", table)
    sla = "Critical (CVSS 9.0–10.0) — 14 calendar days; High (CVSS 7.0–8.9) — 30 calendar days"
    assert gap_analysis._quote_in_chunk("Critical (CVSS 9.0-10.0) - 14 calendar days; High (CVSS 7.0-8.9)", sla)
    # different words still do not match
    assert not gap_analysis._quote_in_chunk("Data encryption keys are rotated every 12 months, consistent with", chunk)
    from app.ai.agents.document_weaknesses import _normalise
    assert _normalise("**Data** — keys; rotated!") == "data keys rotated"
