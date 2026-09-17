"""Scoping agent — runs the Q&A loop against the fast profile."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.ai.prompts import load as load_prompt
from app.ai.router import LLMClient, call_structured
from app.models import Assessment, DescriptionTurn, ServiceDescription
from app.schemas.ai import ScopingTurnOut

SYSTEM_PROMPT = load_prompt("scoping")


def _build_messages(description: ServiceDescription) -> list[dict]:
    history_lines = []
    for t in description.turns:
        role = "Analyst" if t.role == "user" else "Reviewer"
        history_lines.append(f"{role}: {t.content}")

    user_block = (
        f"# Initial description\n{description.text}\n\n"
        f"# Q&A so far\n" + ("\n".join(history_lines) if history_lines else "(none)")
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_block},
    ]


async def run_turn(
    db: Session,
    assessment: Assessment,
    user_message: str | None,
    *,
    client: LLMClient | None = None,
) -> ScopingTurnOut:
    if assessment.description is None:
        raise ValueError("Assessment has no service_description yet.")
    desc = assessment.description

    if user_message is not None and user_message.strip():
        db.add(
            DescriptionTurn(
                description_id=desc.id, role="user", content=user_message.strip()
            )
        )
        db.flush()
        db.refresh(desc)

    out: ScopingTurnOut = await call_structured(
        db,
        purpose="scoping_turn",
        profile="fast",
        messages=_build_messages(desc),
        schema=ScopingTurnOut,
        assessment_id=assessment.id,
        model_override=(assessment.model_overrides or {}).get("scoping"),
        client=client,
    )

    desc.sufficiency_json = out.sufficiency_breakdown.model_dump()
    desc.is_sufficient = bool(out.is_sufficient)
    if out.next_question and not out.is_sufficient:
        db.add(
            DescriptionTurn(
                description_id=desc.id, role="ai", content=out.next_question
            )
        )
    db.commit()
    return out
