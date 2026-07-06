"""Executive summary agent: reference validation, persistence, staleness."""

from __future__ import annotations

import pytest

from app.ai.agents import executive_summary
from app.db import SessionLocal
from app.models import Assessment, ExpectedControl, Scenario, Weakness


def _seed(db):
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
        severity="high",
        description="No MFA on admin portal",
        unmatched=False,
        mapped_control_codes=["IAM.MFA"],
        dedupe_key="w-1",
    )
    db.add(w)
    db.commit()
    db.refresh(a)
    return a, s, w


@pytest.mark.asyncio
async def test_executive_summary_strips_invented_references(fresh_db, fake_client):
    with SessionLocal() as db:
        a, s, w = _seed(db)
        fake_client.push_json(
            {
                "verdict": "Residual risk is High, driven by weak admin authentication.",
                "key_risks": [
                    {
                        "title": "Admin account takeover",
                        "why_it_matters": "Attackers can reach all tenant data.",
                        "scenario_codes": ["DATA_LEAK", "INVENTED_SCEN"],
                        "weakness_ids": [w.id, 99999],
                        "evidence_basis": "Pentest finding, strong evidence.",
                    }
                ],
                "limitations": ["No SOC 2 report was provided."],
                "recommended_actions": [
                    {
                        "action": "Enforce MFA on all administrative accounts.",
                        "priority": "immediate",
                        "related_scenario_codes": ["DATA_LEAK"],
                    }
                ],
            }
        )
        out = await executive_summary.write(db, a.id, client=fake_client)

        (risk,) = out.key_risks
        assert risk.scenario_codes == ["DATA_LEAK"]
        assert risk.weakness_ids == [w.id]
        # Removals are surfaced as limitations, not silently dropped.
        assert any("INVENTED_SCEN" in l for l in out.limitations)
        assert any("99999" in l for l in out.limitations)
        assert "No SOC 2 report was provided." in out.limitations

        db.refresh(a)
        blob = a.executive_summary
        assert blob is not None
        assert blob["summary"]["verdict"].startswith("Residual risk is High")
        assert blob["model_id"]
        assert blob["fingerprint"] == executive_summary.compute_fingerprint(a)


@pytest.mark.asyncio
async def test_fingerprint_changes_when_scored_state_changes(fresh_db, fake_client):
    with SessionLocal() as db:
        a, s, w = _seed(db)
        before = executive_summary.compute_fingerprint(a)
        w.severity = "critical"
        db.commit()
        db.refresh(a)
        assert executive_summary.compute_fingerprint(a) != before
