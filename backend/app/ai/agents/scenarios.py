"""Scenario generator — runs against the reasoner profile."""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy.orm import Session

from app.ai.prompts import load as load_prompt
from app.ai.router import OpenRouterClient, call_structured
from app.models import Assessment, ExpectedControl, Scenario
from app.schemas.ai import ScenarioListOut

SYSTEM_PROMPT = load_prompt("scenarios")
_CATALOG_PATH = Path(__file__).parent.parent / "catalog" / "controls.json"


def _catalog_lines() -> str:
    data = json.loads(_CATALOG_PATH.read_text(encoding="utf-8"))
    return "\n".join(f"- {c['code']} — {c['name']} ({c['family']})" for c in data["controls"])


def _build_messages(description_summary: str, vendor_name: str) -> list[dict]:
    user_block = (
        f"# Vendor\n{vendor_name}\n\n"
        f"# Service description (final)\n{description_summary}\n\n"
        f"# Control catalogue (use these codes when applicable)\n{_catalog_lines()}\n\n"
        "Generate scenarios per the schema."
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_block},
    ]


async def generate(
    db: Session,
    assessment: Assessment,
    description_summary: str,
    *,
    client: OpenRouterClient | None = None,
) -> ScenarioListOut:
    out: ScenarioListOut = await call_structured(
        db,
        purpose="scenario_generation",
        profile="reasoner",
        messages=_build_messages(description_summary, assessment.vendor_name),
        schema=ScenarioListOut,
        assessment_id=assessment.id,
        model_override=(assessment.model_overrides or {}).get("scenarios"),
        max_tokens=4096,
        client=client,
    )

    # Idempotent persistence: clear existing description-sourced scenarios first.
    existing = (
        db.query(Scenario)
        .filter(Scenario.assessment_id == assessment.id, Scenario.source == "description")
        .all()
    )
    for s in existing:
        db.delete(s)
    db.flush()

    for s_out in out.scenarios:
        s = Scenario(
            assessment_id=assessment.id,
            code=s_out.code,
            name=s_out.name,
            description=s_out.description,
            source="description",
            inherent_impact=s_out.inherent_impact,
            inherent_likelihood=s_out.inherent_likelihood,
            residual_impact=s_out.inherent_impact,
            residual_likelihood=s_out.inherent_likelihood,
            score_band="Moderate",
        )
        db.add(s)
        db.flush()
        for c_out in s_out.expected_controls:
            db.add(
                ExpectedControl(
                    scenario_id=s.id,
                    code=c_out.code,
                    name=c_out.name,
                    description=c_out.description,
                    weight=c_out.weight,
                    rationale=c_out.rationale,
                )
            )
    db.commit()
    return out
