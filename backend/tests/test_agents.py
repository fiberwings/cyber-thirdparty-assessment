"""Unit tests for each agent against a fake OpenRouter client."""

from __future__ import annotations

import pytest

from app.ai.agents import (
    cross_correlation,
    document_weaknesses,
    gap_analysis,
    scenarios,
    scoping,
)
from app.db import SessionLocal
from app.models import (
    Assessment,
    Chunk,
    Document,
    ExpectedControl,
    MetaIssue,
    Scenario,
    ServiceDescription,
    Weakness,
)


@pytest.mark.asyncio
async def test_scoping_agent_persists_breakdown_and_question(fresh_db, fake_client):
    fake_client.push_json(
        {
            "is_sufficient": False,
            "sufficiency_breakdown": {
                "data_types": 4,
                "hosting": 2,
                "network_access": 1,
                "identity_flow": 4,
                "regulatory_scope": 4,
                "geography": 4,
                "criticality": 4,
            },
            "missing_dimensions": ["hosting", "network_access"],
            "next_question": "Is the vendor SaaS or self-hosted, and does it require a VPN tunnel into our network?",
            "summary_so_far": "Vendor processes billing PII for the EU region.",
        }
    )

    with SessionLocal() as db:
        a = Assessment(vendor_name="Acme")
        db.add(a)
        db.flush()
        d = ServiceDescription(assessment_id=a.id, text="Acme handles our billing.")
        db.add(d)
        db.commit()
        db.refresh(a)

        out = await scoping.run_turn(db, a, "Acme handles our billing.", client=fake_client)
        assert not out.is_sufficient
        assert "VPN" in out.next_question
        db.refresh(a)
        assert a.description.is_sufficient is False
        assert a.description.sufficiency_json["hosting"] == 2
        assert any(t.role == "ai" for t in a.description.turns)


@pytest.mark.asyncio
async def test_scenario_generator_persists(fresh_db, fake_client):
    # Phase 1: skeletons.
    fake_client.push_json(
        {
            "scenarios": [
                {
                    "code": "DATA_LEAK",
                    "name": "Data leakage of customer PII",
                    "description": "Vendor exfiltrates billing PII.",
                    "inherent_impact": 3,
                    "inherent_likelihood": 3,
                }
            ]
        }
    )
    # Phase 2: controls for DATA_LEAK.
    fake_client.push_json(
        {
            "expected_controls": [
                {"code": "ENC.REST", "name": "Encryption at rest", "description": "", "weight": 1.0, "rationale": ""},
                {"code": "IAM.MFA", "name": "MFA", "description": "", "weight": 1.0, "rationale": ""},
            ]
        }
    )
    with SessionLocal() as db:
        a = Assessment(vendor_name="Acme")
        db.add(a)
        db.flush()
        await scenarios.generate(db, a, "Vendor processes EU PII.", client=fake_client)
        db.refresh(a)
        assert len(a.scenarios) == 1
        s = a.scenarios[0]
        assert s.code == "DATA_LEAK"
        assert len(s.expected_controls) == 2


@pytest.mark.asyncio
async def test_gap_analysis_requires_citation_and_retries(fresh_db, fake_client):
    """First response is invalid (full coverage but no citations); second succeeds."""
    # Bad first response: coverage=full but no citations → Pydantic rejects
    fake_client.push_json(
        {
            "control_code": "IAM.MFA",
            "coverage": "full",
            "effectiveness": "strong",
            "citations": [],
            "rationale": "MFA is mandatory.",
            "meta_flags": [],
        }
    )
    # Good retry
    fake_client.push_json(
        {
            "control_code": "IAM.MFA",
            "coverage": "full",
            "effectiveness": "strong",
            "citations": [
                {
                    "document_id": 1,
                    "page": 7,
                    "section_path": "CC6.1",
                    "quote": "All administrative users must authenticate with MFA.",
                }
            ],
            "rationale": "MFA is mandatory per CC6.1.",
            "meta_flags": [],
        }
    )

    with SessionLocal() as db:
        a = Assessment(vendor_name="Acme")
        db.add(a)
        db.flush()

        # Build a scenario with one expected control, plus an evidence chunk.
        from app.models import ExpectedControl, Scenario
        s = Scenario(
            assessment_id=a.id, code="DATA_LEAK", name="x", description="x",
            inherent_impact=3, inherent_likelihood=3,
        )
        db.add(s)
        db.flush()
        ec = ExpectedControl(scenario_id=s.id, code="IAM.MFA", name="MFA")
        db.add(ec)

        doc = Document(assessment_id=a.id, kind="soc", filename="soc.pdf",
                        mime="application/pdf", sha256="abc", size_bytes=100)
        db.add(doc)
        db.flush()
        ch = Chunk(document_id=doc.id, page=7, section_path="CC6.1",
                    text="All administrative users must authenticate with MFA.")
        db.add(ch)
        db.commit()
        db.refresh(a)
        db.refresh(s)
        db.refresh(ec)

        out = await gap_analysis.assess_control(db, a, s, ec, client=fake_client)
        assert out.coverage == "full"
        # The retried + persisted assessment should have one evidence row
        db.refresh(ec)
        assert ec.assessment is not None
        assert ec.assessment.coverage == "full"
        assert len(ec.assessment.evidence) == 1


def _gap_fixture(db):
    """Assessment + scenario + control + two chunks for citation-binding tests."""
    a = Assessment(vendor_name="Acme")
    db.add(a)
    db.flush()
    s = Scenario(
        assessment_id=a.id, code="DATA_LEAK", name="x", description="x",
        inherent_impact=3, inherent_likelihood=3,
    )
    db.add(s)
    db.flush()
    ec = ExpectedControl(scenario_id=s.id, code="IAM.MFA", name="MFA")
    db.add(ec)
    doc = Document(assessment_id=a.id, kind="soc", filename="soc.pdf",
                   mime="application/pdf", sha256="abc", size_bytes=100)
    db.add(doc)
    db.flush()
    db.add(Chunk(document_id=doc.id, page=7, section_path="CC6.1", ord=1,
                 text="Access reviews are performed quarterly for MFA and admin accounts."))
    db.add(Chunk(document_id=doc.id, page=9, section_path="CC6.2", ord=2,
                 text="All administrative users must authenticate with MFA on every login."))
    db.commit()
    db.refresh(a)
    db.refresh(s)
    db.refresh(ec)
    return a, s, ec, doc


@pytest.mark.asyncio
async def test_gap_analysis_binds_citation_to_chunk_containing_quote(fresh_db, fake_client):
    """A citation without chunk_id binds to the chunk that actually contains
    the quote — not the first chunk of the document."""
    with SessionLocal() as db:
        a, s, ec, doc = _gap_fixture(db)
        fake_client.push_json(
            {
                "control_code": "IAM.MFA",
                "coverage": "full",
                "effectiveness": "strong",
                "citations": [
                    {
                        "document_id": doc.id,
                        "page": 9,
                        "section_path": "CC6.2",
                        "quote": "administrative users must authenticate with MFA",
                    }
                ],
                "rationale": "MFA is enforced.",
                "meta_flags": [],
            }
        )
        await gap_analysis.assess_control(db, a, s, ec, client=fake_client)
        db.refresh(ec)
        (ev,) = ec.assessment.evidence
        assert ev.chunk.page == 9  # the chunk with the quote, not page 7
        assert ec.assessment.unresolved_citations == []


@pytest.mark.asyncio
async def test_gap_analysis_unlocatable_quote_is_unresolved_not_misbound(fresh_db, fake_client):
    """A quote that appears in no chunk must never produce an evidence row;
    it is preserved verbatim in unresolved_citations."""
    with SessionLocal() as db:
        a, s, ec, doc = _gap_fixture(db)
        fake_client.push_json(
            {
                "control_code": "IAM.MFA",
                "coverage": "partial",
                "effectiveness": "adequate",
                "citations": [
                    {
                        "document_id": doc.id,
                        "page": 3,
                        "section_path": "CC1.1",
                        "quote": "This sentence does not exist anywhere in the document.",
                    }
                ],
                "rationale": "Some coverage claimed.",
                "meta_flags": [],
            }
        )
        await gap_analysis.assess_control(db, a, s, ec, client=fake_client)
        db.refresh(ec)
        assert ec.assessment.evidence == []
        (unres,) = ec.assessment.unresolved_citations
        assert unres["quote"] == "This sentence does not exist anywhere in the document."
        assert unres["document_id"] == doc.id


@pytest.mark.asyncio
async def test_gap_analysis_second_retrieval_pass(fresh_db, fake_client):
    """coverage=none + proposed_queries triggers one extra FTS pass and one
    final re-assessment on the combined evidence; the round-2 verdict wins."""
    from app.models import ModelCall

    with SessionLocal() as db:
        a, s, ec, doc = _gap_fixture(db)
        # A chunk round-1 retrieval misses (no 'IAM'/'MFA'/scenario terms) but
        # the model's proposed query finds.
        db.add(Chunk(document_id=doc.id, page=14, section_path="A.9", ord=3,
                     text="Privileged accounts are reviewed every quarter by the security team."))
        db.commit()

        fake_client.push_json(
            {
                "control_code": "IAM.MFA",
                "coverage": "none",
                "effectiveness": "unknown",
                "citations": [],
                "rationale": "No evidence in the candidates.",
                "meta_flags": ["insufficient_info"],
                "proposed_queries": ["privileged accounts quarter security team"],
            }
        )
        fake_client.push_json(
            {
                "control_code": "IAM.MFA",
                "coverage": "partial",
                "effectiveness": "adequate",
                "citations": [
                    {
                        "document_id": doc.id,
                        "page": 14,
                        "section_path": "A.9",
                        "quote": "Privileged accounts are reviewed every quarter",
                    }
                ],
                "rationale": "Quarterly review found on second pass.",
                "meta_flags": [],
                "proposed_queries": [],
            }
        )

        out = await gap_analysis.assess_control(db, a, s, ec, client=fake_client)
        assert out.coverage == "partial"
        db.refresh(ec)
        assert ec.assessment.coverage == "partial"
        (ev,) = ec.assessment.evidence
        assert ev.chunk.page == 14

        purposes = [m.purpose for m in db.query(ModelCall).order_by(ModelCall.id).all()]
        assert purposes == ["gap_analysis_control", "gap_analysis_control_r2"]


@pytest.mark.asyncio
async def test_gap_analysis_rerun_does_not_stack_meta_issues(fresh_db, fake_client):
    """Re-running gap analysis on the same control replaces its meta issues
    instead of duplicating them (duplicates would inflate meta uplift)."""
    canned = {
        "control_code": "IAM.MFA",
        "coverage": "none",
        "effectiveness": "unknown",
        "citations": [],
        "rationale": "No evidence found.",
        "meta_flags": ["insufficient_info"],
    }
    with SessionLocal() as db:
        a, s, ec, doc = _gap_fixture(db)
        fake_client.push_json(canned)
        await gap_analysis.assess_control(db, a, s, ec, client=fake_client)
        db.refresh(ec)  # each production worker loads the control in a fresh session
        fake_client.push_json(canned)
        await gap_analysis.assess_control(db, a, s, ec, client=fake_client)
        issues = db.query(MetaIssue).filter(MetaIssue.assessment_id == a.id).all()
        assert len(issues) == 1
        assert issues[0].kind == "insufficient_info"


@pytest.mark.asyncio
async def test_document_weakness_extraction_persists(fresh_db, fake_client):
    """Per-document extraction agent: single-call default path with two findings."""
    fake_client.push_json(
        {
            "weaknesses": [
                {
                    "severity": "high",
                    "description": "JWT signature is not verified, allowing token forgery.",
                    "quote": "JWT signature not verified",
                    "section_path": "4. Findings / 4.7 Authentication",
                    "page": 12,
                    "kind_signal": "pentest_finding",
                    "suggested_control_codes": ["IAM.MFA"],
                },
                {
                    "severity": "medium",
                    "description": "TLS 1.0 still enabled on a legacy endpoint.",
                    "quote": "TLS 1.0 enabled on /legacy",
                    "section_path": "4. Findings / 4.3 Transport",
                    "page": 8,
                    "kind_signal": "pentest_finding",
                    "suggested_control_codes": ["ENC.TRANSIT"],
                },
            ]
        }
    )
    with SessionLocal() as db:
        a = Assessment(vendor_name="Acme")
        db.add(a)
        db.flush()
        doc = Document(
            assessment_id=a.id,
            kind="pentest",
            filename="pentest.pdf",
            mime="application/pdf",
            sha256="abc",
            size_bytes=1,
        )
        db.add(doc)
        db.flush()
        db.add(
            Chunk(
                document_id=doc.id,
                page=12,
                section_path="4. Findings / 4.7 Authentication",
                ord=1,
                text="The JWT signature was not verified, allowing token forgery via algorithm substitution.",
            )
        )
        db.add(
            Chunk(
                document_id=doc.id,
                page=8,
                section_path="4. Findings / 4.3 Transport",
                ord=2,
                text="TLS 1.0 is enabled on /legacy endpoint.",
            )
        )
        db.commit()
        doc_id = doc.id

        inserted = await document_weaknesses.extract(db, doc_id, client=fake_client)
        assert inserted == 2

        rows = (
            db.query(Weakness)
            .filter(Weakness.source_document_id == doc_id)
            .order_by(Weakness.id)
            .all()
        )
        assert len(rows) == 2
        assert {r.severity for r in rows} == {"high", "medium"}
        assert all(r.kind_signal == "pentest_finding" for r in rows)
        assert all(r.unmatched is True for r in rows)
        assert all(r.dedupe_key for r in rows)
        # quote-based source_chunk_id resolution should land on the correct chunk
        high = next(r for r in rows if r.severity == "high")
        assert high.source_chunk_id is not None
        assert db.get(Chunk, high.source_chunk_id).page == 12

        db.refresh(doc)
        assert doc.weakness_extracted_at is not None


@pytest.mark.asyncio
async def test_document_weakness_extraction_dedupes_re_run(fresh_db, fake_client):
    """A second run on the same doc with the same canned response is a no-op
    (DB-level uniqueness on dedupe_key)."""
    base_response = {
        "weaknesses": [
            {
                "severity": "medium",
                "description": "Some finding",
                "quote": "verbatim quote",
                "section_path": "Findings",
                "page": 1,
                "kind_signal": "pentest_finding",
                "suggested_control_codes": [],
            }
        ]
    }
    fake_client.push_json(base_response)
    fake_client.push_json(base_response)

    with SessionLocal() as db:
        a = Assessment(vendor_name="Acme")
        db.add(a)
        db.flush()
        doc = Document(
            assessment_id=a.id,
            kind="pentest",
            filename="p.pdf",
            mime="application/pdf",
            sha256="x",
            size_bytes=1,
        )
        db.add(doc)
        db.flush()
        db.add(
            Chunk(
                document_id=doc.id,
                page=1,
                section_path="Findings",
                ord=1,
                text="verbatim quote about something bad",
            )
        )
        db.commit()
        doc_id = doc.id

        first = await document_weaknesses.extract(db, doc_id, client=fake_client)
        second = await document_weaknesses.extract(db, doc_id, client=fake_client)
        assert first == 1
        assert second == 0  # dedupe_key prevents the duplicate
        rows = db.query(Weakness).filter(Weakness.source_document_id == doc_id).all()
        assert len(rows) == 1


@pytest.mark.asyncio
async def test_cross_correlation_maps_and_force_emerges(fresh_db, fake_client):
    """Reasoner pass maps one weakness; deterministic floor force-spawns an
    emergent scenario for an unmapped critical."""
    with SessionLocal() as db:
        a = Assessment(vendor_name="Acme")
        db.add(a)
        db.flush()
        s = Scenario(
            assessment_id=a.id,
            code="DATA_LEAK",
            name="PII leakage",
            description="x",
            inherent_impact=3,
            inherent_likelihood=3,
        )
        db.add(s)
        db.flush()
        db.add(ExpectedControl(scenario_id=s.id, code="IAM.MFA", name="MFA"))

        w1 = Weakness(
            assessment_id=a.id,
            severity="high",
            description="Weak admin auth",
            kind_signal="pentest_finding",
            unmatched=True,
            mapped_control_codes=[],
        )
        w2 = Weakness(
            assessment_id=a.id,
            severity="critical",
            description="No DLP for regulated PII",
            kind_signal="pentest_finding",
            unmatched=True,
            mapped_control_codes=[],
        )
        db.add(w1)
        db.add(w2)
        db.commit()

        # Push fake AFTER inserting so we know the IDs.
        fake_client.push_json(
            {
                "weakness_mappings": [
                    {"weakness_id": w1.id, "mapped_control_codes": ["IAM.MFA"]},
                    {"weakness_id": w2.id, "mapped_control_codes": []},
                ],
                "propose_emergent": None,
                "origin_weakness_ids": [],
            }
        )

        stats = await cross_correlation.run(db, a.id, client=fake_client)
        assert stats["mapped"] == 1
        assert stats["forced_emergent"] == 1

        # Re-query to bypass the parent session cache (cross_correlation
        # commits via a fresh session).
        scenarios = (
            db.query(Scenario).filter(Scenario.assessment_id == a.id).all()
        )
        emergent = [s for s in scenarios if s.source == "emergent_from_weakness"]
        assert len(emergent) == 1
        assert w2.id in emergent[0].origin_weakness_ids

        # Both weaknesses should now be marked matched.
        db.expire_all()
        w1_after = db.get(Weakness, w1.id)
        w2_after = db.get(Weakness, w2.id)
        assert w1_after.unmatched is False
        assert w1_after.mapped_control_codes == ["IAM.MFA"]
        assert w2_after.unmatched is False
        # w2 should now be mapped to the X-REMEDIATE placeholder.
        assert any(
            c.startswith("X-REMEDIATE-") for c in (w2_after.mapped_control_codes or [])
        )


@pytest.mark.asyncio
async def test_cross_correlation_splits_batch_on_truncation(fresh_db, fake_client):
    """When a cluster call truncates even after the router's enlarged-budget
    retry, the batch is split in half and each half correlated separately."""
    with SessionLocal() as db:
        a = Assessment(vendor_name="Acme")
        db.add(a)
        db.flush()
        s = Scenario(
            assessment_id=a.id,
            code="DATA_LEAK",
            name="PII leakage",
            description="x",
            inherent_impact=3,
            inherent_likelihood=3,
        )
        db.add(s)
        db.flush()
        db.add(ExpectedControl(scenario_id=s.id, code="IAM.MFA", name="MFA"))
        ws = []
        for i in range(2):
            w = Weakness(
                assessment_id=a.id,
                severity="medium",
                description=f"finding {i}",
                kind_signal="policy_gap",
                unmatched=True,
                mapped_control_codes=[],
            )
            db.add(w)
            ws.append(w)
        db.commit()

        # Full-batch call truncates twice (initial + router retry), then each
        # half succeeds.
        fake_client.push_truncated("{}")
        fake_client.push_truncated("{}")
        for w in ws:
            fake_client.push_json(
                {
                    "weakness_mappings": [
                        {"weakness_id": w.id, "mapped_control_codes": ["IAM.MFA"]},
                    ],
                    "propose_emergent": None,
                    "origin_weakness_ids": [],
                }
            )

        stats = await cross_correlation.run(db, a.id, client=fake_client)
        assert stats["mapped"] == 2
        assert len(fake_client.calls) == 4

        db.expire_all()
        for w in ws:
            w_after = db.get(Weakness, w.id)
            assert w_after.unmatched is False
            assert w_after.mapped_control_codes == ["IAM.MFA"]


@pytest.mark.asyncio
async def test_cross_correlation_surfaces_catalogue_only_mapping(fresh_db, fake_client):
    """A medium weakness the model maps to a catalogue code that is not on any
    scenario must stay unmatched but be surfaced: proposal preserved as a
    suggestion + an `unscored_finding` MetaIssue, exactly once across reruns."""
    with SessionLocal() as db:
        a = Assessment(vendor_name="Acme")
        db.add(a)
        db.flush()
        s = Scenario(
            assessment_id=a.id,
            code="DATA_LEAK",
            name="PII leakage",
            description="x",
            inherent_impact=3,
            inherent_likelihood=3,
        )
        db.add(s)
        db.flush()
        db.add(ExpectedControl(scenario_id=s.id, code="IAM.MFA", name="MFA"))

        w = Weakness(
            assessment_id=a.id,
            severity="medium",
            description="Backups are not encrypted",
            kind_signal="policy_gap",
            unmatched=True,
            mapped_control_codes=[],
        )
        db.add(w)
        db.commit()

        # ENC.BACKUP is a catalogue-style code that is NOT on any scenario.
        canned = {
            "weakness_mappings": [
                {"weakness_id": w.id, "mapped_control_codes": ["ENC.BACKUP"]},
            ],
            "propose_emergent": None,
            "origin_weakness_ids": [],
        }
        fake_client.push_json(canned)
        stats = await cross_correlation.run(db, a.id, client=fake_client)
        assert stats["mapped"] == 0
        assert stats["unscored"] == 1
        assert stats["forced_emergent"] == 0  # medium never hits the floor

        db.expire_all()
        w_after = db.get(Weakness, w.id)
        assert w_after.unmatched is True  # never enters scoring
        assert w_after.mapped_control_codes == ["ENC.BACKUP"]  # suggestion kept

        issues = (
            db.query(MetaIssue)
            .filter(
                MetaIssue.assessment_id == a.id,
                MetaIssue.kind == "unscored_finding",
            )
            .all()
        )
        assert len(issues) == 1
        assert issues[0].target_ref == f"weakness:{w.id}"
        assert issues[0].scenario_code is None  # non-scoring by construction
        assert issues[0].weight == 0.0

        # Re-run: still unmatched, no duplicate MetaIssue.
        fake_client.push_json(canned)
        await cross_correlation.run(db, a.id, client=fake_client)
        issues = (
            db.query(MetaIssue)
            .filter(
                MetaIssue.assessment_id == a.id,
                MetaIssue.kind == "unscored_finding",
            )
            .all()
        )
        assert len(issues) == 1


# ---------- Cross-document contradictions (gap analysis → weakness) ----------

def _contradiction_fixture(db):
    """Assessment with policy + questionnaire that disagree on key rotation,
    and a scenario carrying two encryption controls."""
    a = Assessment(vendor_name="OrbitClear")
    db.add(a)
    db.flush()
    s = Scenario(
        assessment_id=a.id, code="STORAGE_EXPOSURE", name="x", description="x",
        inherent_impact=3, inherent_likelihood=3,
    )
    db.add(s)
    db.flush()
    ec_rest = ExpectedControl(scenario_id=s.id, code="ENC.REST", name="Encryption at rest")
    ec_key = ExpectedControl(scenario_id=s.id, code="ENC.KEY", name="Key management")
    db.add_all([ec_rest, ec_key])
    policy = Document(assessment_id=a.id, kind="policy", filename="policy.pdf",
                      mime="application/pdf", sha256="p", size_bytes=100)
    sig = Document(assessment_id=a.id, kind="questionnaire", filename="sig.xlsx",
                   mime="application/xlsx", sha256="q", size_bytes=100)
    db.add_all([policy, sig])
    db.flush()
    db.add(Chunk(document_id=policy.id, page=12, section_path="7.3 Key Management", ord=1,
                 text="Data encryption keys shall be rotated at least every 12 months."))
    db.add(Chunk(document_id=sig.id, page=None, section_path="EN-04", ord=1,
                 text="Q#: EN-04 | Response: Yes | Encryption keys are rotated every 3 years."))
    db.commit()
    for o in (a, s, ec_rest, ec_key):
        db.refresh(o)
    return a, s, ec_rest, ec_key, policy, sig


def _contradiction_payload(code, *, policy_id, sig_id, severity="medium"):
    return {
        "control_code": code,
        "coverage": "full",
        "effectiveness": "adequate",
        "citations": [
            {"document_id": policy_id, "section_path": "7.3 Key Management",
             "quote": "Data encryption keys shall be rotated at least every 12 months."}
        ],
        "rationale": "Encryption exists but rotation cadence is stated inconsistently.",
        "meta_flags": [],
        "contradictions": [
            {
                "severity": severity,
                "description": "Policy requires annual key rotation; the SIG response states every 3 years.",
                "claims": [
                    {"document_id": policy_id, "section_path": "7.3 Key Management",
                     "quote": "Data encryption keys shall be rotated at least every 12 months."},
                    {"document_id": sig_id, "section_path": "EN-04",
                     "quote": "Encryption keys are rotated every 3 years."},
                ],
            }
        ],
    }


def test_conflicting_evidence_meta_flag_is_rejected():
    """The retired meta-flag can no longer be produced: a contradiction must
    come through `contradictions` so it is scored as a finding."""
    from pydantic import ValidationError
    from app.schemas.ai import ControlAssessmentOut

    with pytest.raises(ValidationError):
        ControlAssessmentOut(
            control_code="X", coverage="none", effectiveness="unknown",
            citations=[], rationale="r", meta_flags=["conflicting_evidence"],
        )


def test_contradiction_requires_two_distinct_sources():
    from pydantic import ValidationError
    from app.schemas.ai import ContradictionOut

    with pytest.raises(ValidationError):
        ContradictionOut(
            severity="medium", description="same place twice",
            claims=[
                {"document_id": 1, "section_path": "A", "quote": "keys rotate yearly"},
                {"document_id": 1, "section_path": "A", "quote": "keys rotate every 3 years"},
            ],
        )


@pytest.mark.asyncio
async def test_gap_analysis_contradiction_becomes_weakness(fresh_db, fake_client):
    from app.models import ControlEvidence

    with SessionLocal() as db:
        a, s, ec_rest, _, policy, sig = _contradiction_fixture(db)
        fake_client.push_json(_contradiction_payload("ENC.REST", policy_id=policy.id, sig_id=sig.id))
        await gap_analysis.assess_control(db, a, s, ec_rest, client=fake_client)

        rows = db.query(Weakness).filter(Weakness.assessment_id == a.id).all()
        assert len(rows) == 1
        w = rows[0]
        assert w.kind_signal == "cross_doc_conflict"
        assert w.origin == "gap_analysis"
        assert w.unmatched is False
        assert w.mapped_control_codes == ["ENC.REST"]
        assert w.origin_refs == ["STORAGE_EXPOSURE/ENC.REST"]
        assert w.source_document_id == policy.id and w.source_chunk_id is not None
        # Both sides resolved to real chunks.
        assert [r["document_id"] for r in w.evidence_refs] == [policy.id, sig.id]
        assert all(r["chunk_id"] for r in w.evidence_refs)
        # Both quotes are attached to the control as contradicting evidence.
        contra = (
            db.query(ControlEvidence)
            .filter(ControlEvidence.polarity == "contradicts")
            .all()
        )
        assert len(contra) == 2
        # And *no* meta-issue was written for the same fact.
        assert db.query(MetaIssue).filter(MetaIssue.assessment_id == a.id).count() == 0


@pytest.mark.asyncio
async def test_gap_analysis_same_contradiction_two_controls_one_row(fresh_db, fake_client):
    from app.api.scoring import _recalculate_in_session

    with SessionLocal() as db:
        a, s, ec_rest, ec_key, policy, sig = _contradiction_fixture(db)
        fake_client.push_json(_contradiction_payload("ENC.REST", policy_id=policy.id, sig_id=sig.id))
        await gap_analysis.assess_control(db, a, s, ec_rest, client=fake_client)
        fake_client.push_json(
            _contradiction_payload("ENC.KEY", policy_id=policy.id, sig_id=sig.id, severity="high")
        )
        await gap_analysis.assess_control(db, a, s, ec_key, client=fake_client)

        rows = db.query(Weakness).filter(Weakness.assessment_id == a.id).all()
        assert len(rows) == 1
        w = rows[0]
        assert w.mapped_control_codes == ["ENC.KEY", "ENC.REST"]
        assert sorted(w.origin_refs) == ["STORAGE_EXPOSURE/ENC.KEY", "STORAGE_EXPOSURE/ENC.REST"]
        assert w.severity == "high"  # highest sighting wins

        # Scores once in the scenario even though it maps to two of its controls.
        db.refresh(a)
        scores, _agg = _recalculate_in_session(db, a)
        sc = next(x for x in scores if x.code == "STORAGE_EXPOSURE")
        assert sc.weakness_uplift_raw == pytest.approx(0.75)


@pytest.mark.asyncio
async def test_gap_analysis_contradiction_rerun_is_idempotent(fresh_db, fake_client):
    with SessionLocal() as db:
        a, s, ec_rest, ec_key, policy, sig = _contradiction_fixture(db)
        payload = _contradiction_payload("ENC.REST", policy_id=policy.id, sig_id=sig.id)
        fake_client.push_json(payload)
        await gap_analysis.assess_control(db, a, s, ec_rest, client=fake_client)
        db.refresh(ec_rest)
        fake_client.push_json(payload)
        await gap_analysis.assess_control(db, a, s, ec_rest, client=fake_client)

        rows = db.query(Weakness).filter(Weakness.assessment_id == a.id).all()
        assert len(rows) == 1
        assert rows[0].origin_refs == ["STORAGE_EXPOSURE/ENC.REST"]

        # Second control also claims it, then the first control re-runs clean:
        # the row survives on the second control's claim only.
        fake_client.push_json(_contradiction_payload("ENC.KEY", policy_id=policy.id, sig_id=sig.id))
        await gap_analysis.assess_control(db, a, s, ec_key, client=fake_client)
        db.refresh(ec_rest)
        clean = dict(payload, contradictions=[], effectiveness="strong")
        fake_client.push_json(clean)
        await gap_analysis.assess_control(db, a, s, ec_rest, client=fake_client)
        rows = db.query(Weakness).filter(Weakness.assessment_id == a.id).all()
        assert len(rows) == 1
        assert rows[0].origin_refs == ["STORAGE_EXPOSURE/ENC.KEY"]
        assert rows[0].mapped_control_codes == ["ENC.KEY"]

        # Last claimant re-runs clean → row removed (not user-edited).
        db.refresh(ec_key)
        fake_client.push_json(dict(clean, control_code="ENC.KEY"))
        await gap_analysis.assess_control(db, a, s, ec_key, client=fake_client)
        assert db.query(Weakness).filter(Weakness.assessment_id == a.id).count() == 0


@pytest.mark.asyncio
async def test_gap_analysis_skips_contradiction_already_known(fresh_db, fake_client):
    """A contradiction where one side is already the evidence of a weakness
    mapped to this control is that weakness restated against the vendor's
    claim — the failure is already scored once, so no second row."""
    with SessionLocal() as db:
        a, s, ec_rest, _, policy, sig = _contradiction_fixture(db)
        db.add(
            Weakness(
                assessment_id=a.id, source_document_id=sig.id, severity="medium",
                description="Keys rotate only every 3 years.",
                quote="Encryption keys are rotated every 3 years.",
                mapped_control_codes=["ENC.REST"], unmatched=False,
                kind_signal="questionnaire_negative", dedupe_key="k1",
            )
        )
        db.commit()
        fake_client.push_json(_contradiction_payload("ENC.REST", policy_id=policy.id, sig_id=sig.id))
        await gap_analysis.assess_control(db, a, s, ec_rest, client=fake_client)
        rows = db.query(Weakness).filter(Weakness.assessment_id == a.id).all()
        assert len(rows) == 1
        assert not any(w.kind_signal == "cross_doc_conflict" for w in rows)
        # The known weaknesses were shown to the model.
        sent = fake_client.calls[-1]["messages"][1]["content"]
        assert "Known weaknesses already mapped to this control" in sent
        assert "Keys rotate only every 3 years." in sent
