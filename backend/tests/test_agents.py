"""Unit tests for each agent against a fake OpenRouter client."""

from __future__ import annotations

import pytest

from app.ai.agents import gap_analysis, scenarios, scoping, weaknesses
from app.db import SessionLocal
from app.models import Assessment, Chunk, Document, ServiceDescription


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
    fake_client.push_json(
        {
            "scenarios": [
                {
                    "code": "DATA_LEAK",
                    "name": "Data leakage of customer PII",
                    "description": "Vendor exfiltrates billing PII.",
                    "inherent_impact": 3,
                    "inherent_likelihood": 3,
                    "expected_controls": [
                        {"code": "ENC.REST", "name": "Encryption at rest", "description": "", "weight": 1.0, "rationale": ""},
                        {"code": "IAM.MFA", "name": "MFA", "description": "", "weight": 1.0, "rationale": ""},
                    ],
                }
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
async def test_weakness_synthesis_skips_existing_codes(fresh_db, fake_client):
    fake_client.push_json(
        {
            "weaknesses": [
                {
                    "severity": "high",
                    "description": "Pen test found unpatched OpenSSL.",
                    "quote": "OpenSSL 1.0.2 in production.",
                    "citation": {"document_id": 1, "page": 3, "section_path": "Findings", "quote": "OpenSSL 1.0.2 in production."},
                    "mapped_control_codes": ["ENDPOINT.PATCH"],
                    "suggests_emergent_scenario_code": None,
                }
            ],
            "emergent_scenarios": [
                {
                    "code": "DATA_LEAK",
                    "name": "Already exists - ignore",
                    "description": "duplicate",
                    "inherent_impact": 2,
                    "inherent_likelihood": 2,
                    "expected_controls": [{"code": "ENC.REST", "name": "Encryption at rest", "description": "", "weight": 1.0, "rationale": ""}],
                },
                {
                    "code": "CRYPTO_DEPRECATED",
                    "name": "Use of deprecated crypto",
                    "description": "OpenSSL 1.0.2 is EOL.",
                    "inherent_impact": 3,
                    "inherent_likelihood": 3,
                    "expected_controls": [{"code": "ENDPOINT.PATCH", "name": "Patch", "description": "", "weight": 1.0, "rationale": ""}],
                },
            ],
        }
    )
    with SessionLocal() as db:
        from app.models import ExpectedControl, Scenario
        a = Assessment(vendor_name="Acme")
        db.add(a)
        db.flush()
        # Pretend a scenario with code DATA_LEAK already exists.
        s = Scenario(
            assessment_id=a.id, code="DATA_LEAK", name="Existing", description="x",
            inherent_impact=3, inherent_likelihood=3,
        )
        db.add(s)
        db.flush()
        db.add(ExpectedControl(scenario_id=s.id, code="ENC.REST", name="Encryption at rest"))
        # Add a pentest doc + chunk so the agent has something to look at.
        doc = Document(assessment_id=a.id, kind="pentest", filename="p.docx",
                        mime="application/vnd...", sha256="z", size_bytes=10)
        db.add(doc)
        db.flush()
        db.add(Chunk(document_id=doc.id, section_path="Findings",
                      text="OpenSSL 1.0.2 is in production."))
        db.commit()
        db.refresh(a)

        await weaknesses.synthesize(db, a, "summary", client=fake_client)

        db.refresh(a)
        codes = sorted(s.code for s in a.scenarios)
        assert "DATA_LEAK" in codes
        assert "CRYPTO_DEPRECATED" in codes
        # DATA_LEAK should not have been duplicated
        assert codes.count("DATA_LEAK") == 1
        assert len(a.weaknesses) == 1
