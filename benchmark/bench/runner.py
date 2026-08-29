"""Per-case orchestration + batch driver.

Drives the TPRM app end-to-end over HTTP for each (case, repetition), grades
the resulting report with the LLM judge, and persists everything to the
benchmark DB. A failing case records an error row and never aborts the batch.

Stage order matters: scenarios are generated BEFORE documents are uploaded
because cross-correlation maps extracted weaknesses onto existing scenarios'
expected controls (see backend/app/ai/agents/cross_correlation.py).
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from . import collect, prompts, versioning
from .app_client import AppClient, StageError
from .cases import Case
from .config import settings
from .db import get_session
from .judge import Judge, JudgeError
from .metrics import (
    EXEC_WEIGHTS,
    score_exec_rubric,
    score_weakness_matching,
)
from .models import CaseResult, FindingMatch, JudgeCall, Run, utcnow


@dataclass
class RunConfig:
    case_ids: list[str] | None = None
    repetitions: int = 1
    concurrency: int = 1
    cleanup: str = "none"  # none | ok | all
    model_overrides: dict[str, str] = field(default_factory=dict)
    judge_model: str | None = None
    match_confidence_threshold: tuple[str, ...] = settings.MATCH_CONFIDENCE_THRESHOLD
    notes: str = ""

    def to_json(self) -> str:
        return json.dumps(
            {
                "case_ids": self.case_ids,
                "repetitions": self.repetitions,
                "concurrency": self.concurrency,
                "cleanup": self.cleanup,
                "model_overrides": self.model_overrides,
                "judge_model": self.judge_model,
                "match_confidence_threshold": list(self.match_confidence_threshold),
                "exec_weights": EXEC_WEIGHTS,
                "notes": self.notes,
            }
        )


# ---------------- pipeline ----------------


def run_pipeline(client: AppClient, case: Case, overrides: dict[str, str]) -> tuple[int, dict, dict]:
    """Execute the full assessment pipeline for one case.

    Returns (assessment_id, report, stage timings in seconds).
    Raises StageError with the failing stage on any error.
    """
    timings: dict[str, float] = {}

    def timed(stage: str):
        class _T:
            def __enter__(self_):
                self_.t0 = time.monotonic()

            def __exit__(self_, *exc):
                timings[stage] = round(time.monotonic() - self_.t0, 2)
                return False

        return _T()

    with timed("create"):
        assessment_id = client.create_assessment(case.vendor_name)
    try:
        if overrides:
            client.set_model_overrides(assessment_id, overrides)

        with timed("description"):
            client.set_description(assessment_id, case.description)
            client.force_continue_scoping(assessment_id)

        # Scenarios FIRST — correlation maps weaknesses onto their controls.
        with timed("scenarios"):
            task_id = client.generate_scenarios(assessment_id)
            client.wait_task(task_id, "scenarios", settings.TIMEOUT_SCENARIOS, assessment_id)

        # Documents sequentially; await each auto-fired extraction so the
        # auto cross-correlation never races the next upload.
        with timed("documents"):
            for doc in case.documents:
                res = client.upload_document(assessment_id, case.doc_path(doc), doc.kind)
                wtid = res.get("weakness_task_id")
                if wtid:
                    client.wait_task(wtid, "extraction", settings.TIMEOUT_EXTRACTION, assessment_id)

        # Explicit awaited cross-correlation (idempotent; the auto-fired pass
        # exposes no task_id to the client, so this guarantees completion).
        with timed("cross_correlate"):
            task_id = client.cross_correlate(assessment_id)
            client.wait_task(task_id, "cross_correlate", settings.TIMEOUT_CORRELATE, assessment_id)

        with timed("gap_analysis"):
            task_id = client.run_gap_analysis(assessment_id)
            client.wait_task(task_id, "gap_analysis", settings.TIMEOUT_GAP_ANALYSIS, assessment_id)

        with timed("recalculate"):
            client.recalculate(assessment_id)

        with timed("narratives"):
            task_id = client.run_narratives(assessment_id)
            client.wait_task(task_id, "narratives", settings.TIMEOUT_NARRATIVES, assessment_id)

        with timed("report"):
            report = client.get_report(assessment_id)
    except StageError:
        raise
    except Exception as e:  # httpx errors etc. — attribute to the last stage
        raise StageError("pipeline", str(e)) from e

    return assessment_id, report, timings


# ---------------- grading ----------------


def flatten_exec_summary(summary: dict) -> str:
    """Flatten the structured executive summary into judge-readable text."""
    lines = [f"Verdict: {summary.get('verdict', '')}"]
    for kr in summary.get("key_risks", []):
        lines.append(f"Key risk: {kr.get('title', '')} — {kr.get('why_it_matters', '')}")
        if kr.get("evidence_basis"):
            lines.append(f"  Evidence basis: {kr['evidence_basis']}")
    for lim in summary.get("limitations", []):
        lines.append(f"Limitation: {lim}")
    for act in summary.get("recommended_actions", []):
        lines.append(f"Recommended action ({act.get('priority', '')}): {act.get('action', '')}")
    return "\n".join(lines)


def build_evidence_digest(report: dict) -> str:
    """What the assessment actually found — the faithfulness ground truth."""
    agg = report.get("aggregate", {})
    lines = [
        f"Aggregate residual risk: band={agg.get('band')}, rank={agg.get('rank')}"
    ]
    for s in report.get("scenarios", []):
        lines.append(
            f"Scenario {s.get('code')}: {s.get('name')} — band={s.get('band')}, "
            f"residual impact={s.get('residual_impact')}, "
            f"likelihood={s.get('residual_likelihood')}"
        )
    for w in report.get("weaknesses", []):
        lines.append(f"Weakness [{w.get('severity')}] (id {w.get('id')}): {w.get('description')}")
    for mi in report.get("meta_issues", []):
        lines.append(f"Meta issue ({mi.get('kind')}): {mi.get('rationale', '')}")
    return "\n".join(lines)


def grade_case(
    judge: Judge, case: Case, report: dict, confidence_threshold: tuple[str, ...]
) -> tuple[dict, list, list]:
    """Grade one report. Returns (metrics dict, finding-match rows, judge records)."""
    expected = [
        {
            "id": w.id,
            "description": w.description,
            "severity": w.severity,
            "mapped_control_codes": w.mapped_control_codes,
        }
        for w in case.golden.expected_weaknesses
    ]
    expected_by_id = {
        w.id: {"severity": w.severity, "optional": w.optional, "description": w.description}
        for w in case.golden.expected_weaknesses
    }
    actual = [
        {
            "id": w["id"],
            "severity": w.get("severity"),
            "description": w.get("description"),
            "quote": w.get("quote", ""),
            "mapped_control_codes": w.get("mapped_control_codes", []),
        }
        for w in report.get("weaknesses", [])
    ]
    actual_by_id = {a["id"]: a for a in actual}
    actual_severity_by_id = {a["id"]: a.get("severity") for a in actual}

    judge_records: list = []

    match_out, recs = judge.match_weaknesses(expected, actual)
    judge_records.extend(recs)

    scores = score_weakness_matching(
        matches=[m.model_dump() for m in match_out.matches],
        unmatched_expected=[u.model_dump() for u in match_out.unmatched_expected],
        unmatched_actual=[u.model_dump() for u in match_out.unmatched_actual],
        expected_by_id=expected_by_id,
        actual_severity_by_id=actual_severity_by_id,
        confidence_threshold=confidence_threshold,
    )

    # Build persistence rows for the drill-down UI
    match_rows: list[dict] = []
    counted_pairs = {(eid, aid) for eid, aid, _ in scores.counted_matches}
    for m in match_out.matches:
        exp = expected_by_id.get(m.expected_id, {})
        act = actual_by_id.get(m.actual_id, {})
        match_rows.append(
            dict(
                match_type="matched",
                expected_id=m.expected_id,
                expected_description=exp.get("description", ""),
                expected_severity=exp.get("severity"),
                expected_optional=bool(exp.get("optional")),
                actual_weakness_id=m.actual_id,
                actual_description=act.get("description", ""),
                actual_severity=act.get("severity"),
                confidence=m.confidence,
                counted=(m.expected_id, m.actual_id) in counted_pairs,
                justification=m.justification,
            )
        )
    for u in match_out.unmatched_expected:
        exp = expected_by_id.get(u.expected_id, {})
        match_rows.append(
            dict(
                match_type="missed",
                expected_id=u.expected_id,
                expected_description=exp.get("description", ""),
                expected_severity=exp.get("severity"),
                expected_optional=bool(exp.get("optional")),
                confidence=None,
                counted=not exp.get("optional"),
                justification=u.justification,
            )
        )
    for u in match_out.unmatched_actual:
        act = actual_by_id.get(u.actual_id, {})
        match_rows.append(
            dict(
                match_type="extra",
                actual_weakness_id=u.actual_id,
                actual_description=act.get("description", ""),
                actual_severity=act.get("severity"),
                confidence=None,
                counted=True,
                justification=u.justification,
            )
        )

    metrics = {
        "tp": scores.tp,
        "fp": scores.fp,
        "fn": scores.fn,
        "precision": round(scores.precision, 4),
        "recall": round(scores.recall, 4),
        "f1": round(scores.f1, 4),
        "severity_exact": scores.severity_exact,
        "severity_mae": scores.severity_mae,
        "exec_coverage": None,
        "exec_faithfulness": None,
        "exec_violation": None,
        "exec_overall": None,
    }

    # Exec summary rubric (only when the app produced one and a rubric exists)
    rubric = case.golden.exec_summary_rubric
    summary = report.get("executive_summary")
    if summary and (rubric.must_cover or rubric.must_not_claim):
        must_cover = [{"id": p.id, "point": p.point} for p in rubric.must_cover]
        must_not = [{"id": c.id, "claim": c.claim} for c in rubric.must_not_claim]
        rubric_out, recs = judge.grade_exec_summary(
            flatten_exec_summary(summary),
            build_evidence_digest(report),
            must_cover,
            must_not,
        )
        judge_records.extend(recs)
        exec_scores = score_exec_rubric(
            coverage_items=[c.model_dump() for c in rubric_out.coverage],
            violation_items=[v.model_dump() for v in rubric_out.violations],
            faithfulness_score=rubric_out.faithfulness.score,
            n_must_cover=len(must_cover),
            n_must_not_claim=len(must_not),
        )
        metrics.update(
            exec_coverage=exec_scores.coverage,
            exec_faithfulness=exec_scores.faithfulness,
            exec_violation=exec_scores.violation,
            exec_overall=exec_scores.overall,
        )

    return metrics, match_rows, judge_records


# ---------------- batch driver ----------------


def _persist_judge_records(session, case_result_id: int, records: list) -> None:
    for r in records:
        session.add(
            JudgeCall(
                case_result_id=case_result_id,
                purpose=r.purpose,
                model_id=r.model_id,
                prompt_version=r.prompt_version,
                latency_ms=r.latency_ms,
                input_tokens=r.input_tokens,
                output_tokens=r.output_tokens,
                ok=r.ok,
                error=r.error,
                request_json=r.request_json,
                response_json=r.response_json,
            )
        )


def run_one(
    run_id: int,
    case: Case,
    repetition: int,
    config: RunConfig,
    backend_url: str,
) -> str:
    """Run + grade one (case, repetition). Returns the case_result status."""
    client = AppClient(backend_url)
    session = get_session()
    cr = CaseResult(run_id=run_id, case_id=case.id, repetition=repetition)
    session.add(cr)
    session.commit()

    assessment_id = None
    status = "ok"
    try:
        try:
            assessment_id, report, timings = run_pipeline(
                client, case, config.model_overrides
            )
            cr.assessment_id = assessment_id
            cr.timings_json = json.dumps(timings)
            cr.report_json = json.dumps(report)
            agg = report.get("aggregate", {})
            cr.aggregate_band = agg.get("band", "")
            cr.aggregate_rank = agg.get("rank")
        except StageError as e:
            status = "error"
            cr.error_stage = e.stage
            cr.error_detail = e.detail
            raise

        tokens = collect.collect_model_calls(assessment_id)
        if tokens:
            cr.tokens_json = json.dumps(tokens)

        try:
            judge = Judge(model=config.judge_model)
            try:
                metrics, match_rows, judge_records = grade_case(
                    judge, case, report, config.match_confidence_threshold
                )
            finally:
                judge.close()
            for k, v in metrics.items():
                setattr(cr, k, v)
            for row in match_rows:
                session.add(FindingMatch(case_result_id=cr.id, **row))
            _persist_judge_records(session, cr.id, judge_records)
        except JudgeError as e:
            status = "judge_error"
            cr.error_stage = "judge"
            cr.error_detail = str(e)
            _persist_judge_records(session, cr.id, e.records)
    except StageError:
        pass  # already recorded
    except Exception as e:  # never let one case kill the batch
        status = "error"
        cr.error_stage = cr.error_stage or "unexpected"
        cr.error_detail = cr.error_detail or repr(e)
    finally:
        cr.status = status
        cr.finished_at = utcnow()

        # Cleanup policy: keep errored assessments for debugging unless "all"
        if assessment_id is not None and (
            config.cleanup == "all" or (config.cleanup == "ok" and status == "ok")
        ):
            try:
                client.delete_assessment(assessment_id)
                cr.assessment_deleted = True
            except Exception:
                pass

        session.commit()
        session.close()
        client.close()
    return status


def run_batch(cases: list[Case], config: RunConfig) -> tuple[int, str]:
    """Run the full benchmark batch. Returns (run_id, run status)."""
    backend_url = settings.BENCH_BACKEND_URL
    client = AppClient(backend_url)
    if not client.health():
        client.close()
        raise SystemExit(f"backend at {backend_url} is not healthy — start it first")

    sha, dirty = versioning.git_sha()
    session = get_session()
    run = Run(
        backend_url=backend_url,
        app_git_sha=sha,
        app_git_dirty=dirty,
        app_version=versioning.app_version(),
        models_json=json.dumps(versioning.models_snapshot(client, config.model_overrides)),
        judge_model=config.judge_model or settings.JUDGE_MODEL,
        judge_prompt_versions_json=json.dumps(
            {
                "weakness_match": prompts.WEAKNESS_MATCH_VERSION,
                "exec_rubric": prompts.EXEC_RUBRIC_VERSION,
            }
        ),
        config_json=config.to_json(),
        notes=config.notes,
    )
    session.add(run)
    session.commit()
    run_id = run.id
    client.close()

    jobs = [(case, rep) for case in cases for rep in range(1, config.repetitions + 1)]
    statuses: list[str] = []
    if config.concurrency <= 1:
        for case, rep in jobs:
            print(f"[case {case.id} rep {rep}] starting")
            st = run_one(run_id, case, rep, config, backend_url)
            print(f"[case {case.id} rep {rep}] {st}")
            statuses.append(st)
    else:
        with ThreadPoolExecutor(max_workers=config.concurrency) as pool:
            futures = {
                pool.submit(run_one, run_id, case, rep, config, backend_url): (case, rep)
                for case, rep in jobs
            }
            for fut, (case, rep) in futures.items():
                st = fut.result()
                print(f"[case {case.id} rep {rep}] {st}")
                statuses.append(st)

    ok = sum(1 for s in statuses if s == "ok")
    if ok == len(statuses):
        run_status = "done"
    elif ok == 0:
        run_status = "failed"
    else:
        run_status = "partial"

    run.status = run_status
    run.finished_at = utcnow()
    session.add(run)
    session.commit()
    session.close()
    return run_id, run_status
