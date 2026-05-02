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
                    "quote": "All admins must use MFA.",
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
