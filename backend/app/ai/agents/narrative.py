"""Narrative writer for the score-explanation panel."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.ai.context import analysis_date_line
from app.ai.prompts import load as load_prompt
from app.ai.router import OpenRouterClient, call_text
from app.config import settings
from app.models import Assessment, MetaIssue, Scenario
from app.scoring.engine import level_to_name

SYSTEM_PROMPT = load_prompt("narrative")


def _build_messages(scenario: Scenario, meta_issues: list[MetaIssue]) -> list[dict]:
    controls_block = []
    for ec in scenario.expected_controls:
        ca = ec.assessment
        if ca is None:
            controls_block.append(f"- {ec.code} {ec.name} → coverage=none, effectiveness=unknown")
            continue
        cites = []
        for ev in ca.evidence:
            page = ev.chunk.page if ev.chunk and ev.chunk.page else None
            sect = ev.chunk.section_path if ev.chunk else ""
            loc = f"p.{page}" if page else (sect or "section unknown")
            cites.append(f'"{ev.quote[:120]}" ({loc})')
        cite_str = "; ".join(cites) if cites else "no citations"
        controls_block.append(
            f"- {ec.code} {ec.name} → coverage={ca.coverage}, "
            f"effectiveness={ca.effectiveness}; {cite_str}"
        )
    meta_block = "\n".join(
        f"- {m.kind}: {m.rationale or m.target_ref}"
        for m in meta_issues
        if m.scenario_code == scenario.code
    ) or "(none)"

    user_block = (
        f"{analysis_date_line(scenario.assessment)}\n\n"
        f"# Scenario\n{scenario.code} — {scenario.name}\n{scenario.description}\n\n"
        f"# Inherent\nimpact={level_to_name(scenario.inherent_impact)}, "
        f"likelihood={level_to_name(scenario.inherent_likelihood)}\n\n"
        f"# Residual\nimpact={level_to_name(scenario.residual_impact)}, "
        f"likelihood={level_to_name(scenario.residual_likelihood)}, "
        f"band={scenario.score_band}\n\n"
        f"# Expected controls\n" + "\n".join(controls_block) + "\n\n"
        f"# Meta-issues\n{meta_block}\n\n"
        "Write the score-explanation paragraph."
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_block},
    ]


async def write_for_scenario(
    db: Session,
    assessment: Assessment,
    scenario: Scenario,
    *,
    client: OpenRouterClient | None = None,
) -> str:
    meta = list(assessment.meta_issues)
    text = await call_text(
        db,
        purpose="narrative",
        profile="fast",
        messages=_build_messages(scenario, meta),
        assessment_id=assessment.id,
        model_override=(assessment.model_overrides or {}).get("narrative"),
        # Reasoning models spend completion tokens on thinking before the
        # visible paragraph — 400 gets fully consumed before any text lands.
        max_tokens=settings.llm_budget_small,
        client=client,
    )
    scenario.rationale = text.strip()
    db.commit()
    return scenario.rationale
