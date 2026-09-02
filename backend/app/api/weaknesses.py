from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app import workflow
from app.ai.agents import cross_correlation as corr_agent
from app.api.deps import db_session, get_assessment
from app.db import SessionLocal
from app.models import Assessment
from app.schemas.api import TaskStatusRead, WeaknessRead
from app.tasks import mark_phase_done, mark_phase_error, mark_phase_started, registry

router = APIRouter(prefix="/api/assessments", tags=["weaknesses"])


@router.get("/{assessment_id}/weaknesses", response_model=list[WeaknessRead])
def list_weaknesses(
    assessment_id: int, include: str = "confirmed", db: Session = Depends(db_session)
):
    """Reported weaknesses (status confirmed). `include=all` also returns
    candidates, evidence notes, dropped and merged rows with their review."""
    a = get_assessment(assessment_id, db)
    rows = a.all_weaknesses if include == "all" else a.weaknesses
    return [WeaknessRead.model_validate(w) for w in sorted(rows, key=lambda w: w.id)]


@router.post(
    "/{assessment_id}/weaknesses/synthesize",
    response_model=TaskStatusRead,
)
async def synthesize(assessment_id: int, db: Session = Depends(db_session)):
    """Backward-compatible alias for `POST /assessments/{id}/cross-correlate`.

    The original synthesizer (heuristic 60-chunk pool, single global call) was
    retired in favour of the per-document extraction + cross-correlation
    pipeline. The frontend's `synthesizeWeaknesses` action now triggers
    cross-correlation over already-extracted weaknesses.
    """
    a = get_assessment(assessment_id, db)
    return submit_correlation(db, a)


def submit_correlation(db: Session, a: Assessment) -> TaskStatusRead:
    """Shared by `/cross-correlate` and `/weaknesses/synthesize`.

    Requires scoping, scenarios and evidence extraction to be done and current;
    re-attaches to a correlation already in flight. Starting a run stamps gap
    analysis and narratives stale — correlation rewrites the confirmed
    weakness set they were computed from."""
    workflow.require_step_ready(a, "correlation")
    live = workflow.require_no_run_in_flight(a, reattach_kind=workflow.KIND_CORRELATION)
    if live is not None:
        return workflow.reattach_response(live)
    workflow.invalidate_downstream(a, "analysis", reason="Cross-correlation re-run")
    db.commit()
    aid = a.id

    async def job(handle):
        await handle.update(progress=0.05, detail="Correlating weaknesses...")

        async def on_progress(p: float, detail: str):
            await handle.update(progress=p, detail=detail)

        try:
            with SessionLocal() as inner:
                await corr_agent.run(inner, aid, on_progress=on_progress)
            mark_phase_done(aid, "cross_correlation")
        except Exception as e:
            mark_phase_error(aid, "cross_correlation", str(e))
            raise

    handle = registry.submit(job, kind=workflow.KIND_CORRELATION, assessment_id=aid)
    mark_phase_started(aid, "cross_correlation", handle.id)
    return workflow.reattach_response(handle)
