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
    band_error,
    score_classification,
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
    # A backend with the dev LLM cache on would serve stored responses; a
    # measurement run refuses it unless explicitly allowed (plumbing tests).
    allow_dev_cache: bool = False
    judge_model: str | None = None
    # none  = no judge calls (deterministic metrics only: band_error, n_weaknesses)
    # match = weakness matching only (P/R/F1)
    # full  = matching + signal/noise classification + exec-summary rubric
    judge_mode: str = "full"
    skip_narratives: bool = False
    # "run" = drive the pipeline then grade; "grade" = grade a stored assessment
    mode: str = "run"
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
                "mode": self.mode,
                "allow_dev_cache": self.allow_dev_cache,
                "judge_mode": self.judge_mode,
                "skip_narratives": self.skip_narratives,
                "judge_classify_chunk_budget_chars": settings.JUDGE_CLASSIFY_CHUNK_BUDGET_CHARS,
                "match_confidence_threshold": list(self.match_confidence_threshold),
                "exec_weights": EXEC_WEIGHTS,
                "notes": self.notes,
            }
        )


# ---------------- pipeline ----------------


def run_pipeline(
    client: AppClient,
    case: Case,
    overrides: dict[str, str],
    skip_narratives: bool = False,
) -> tuple[int, dict, dict]:
    """Execute the full assessment pipeline for one case.

    skip_narratives leaves out the narratives + executive-summary stage
    (~30k tokens); the report then carries no executive_summary and the
    exec rubric is not graded.

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
        with timed("settings"):
            client.set_settings(assessment_id, case.as_of_date, case.standards_profile)

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

        if not skip_narratives:
            with timed("narratives"):
                task_id = client.run_narratives(assessment_id)
                client.wait_task(task_id, "narratives", settings.TIMEOUT_NARRATIVES, assessment_id)

        with timed("report"):
            report = client.get_report(assessment_id)
    except StageError as e:
        e.assessment_id = assessment_id
        raise
    except Exception as e:  # httpx errors etc. — attribute to the last stage
        raise StageError("pipeline", str(e), assessment_id) from e

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


def deterministic_metrics(case: Case, report: dict) -> dict:
    """Metrics that need no judge: reported-weakness count and band error."""
    agg = report.get("aggregate", {})
    return {
        "n_weaknesses": len(report.get("weaknesses", [])),
        "band_error": band_error(agg.get("band"), case.golden.expected_band),
    }


def select_chunks_for_classification(
    report: dict, all_chunks: list[dict], budget_chars: int
) -> tuple[list[dict], str]:
    """Whole bundle when it fits the budget, else only the chunks the
    weaknesses cite (source_chunk_id + evidence_refs). Returns (chunks, scope)."""
    slim = [
        {
            "chunk_id": c["id"],
            "document_id": c["document_id"],
            "page": c.get("page"),
            "section_path": c.get("section_path", ""),
            "text": c.get("text", ""),
        }
        for c in all_chunks
    ]
    total = sum(len(c["text"]) for c in slim)
    if total <= budget_chars:
        return slim, "full"
    cited: set[int] = set()
    for w in report.get("weaknesses", []):
        if w.get("source_chunk_id"):
            cited.add(w["source_chunk_id"])
        for ref in w.get("evidence_refs") or []:
            if isinstance(ref, dict) and ref.get("chunk_id"):
                cited.add(ref["chunk_id"])
    return [c for c in slim if c["chunk_id"] in cited], "cited"


def grade_case(
    judge: Judge,
    case: Case,
    report: dict,
    confidence_threshold: tuple[str, ...],
    judge_mode: str = "full",
    chunks: list[dict] | None = None,
) -> tuple[dict, list, list]:
    """Grade one report. Returns (metrics dict, finding-match rows, judge records).

    judge_mode "match" runs the weakness matching only; "full" adds the
    signal/noise classification (needs the evidence chunks) and the
    exec-summary rubric when the report carries a summary.
    """
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
    # Richer view for the classifier: where each finding came from.
    actual_for_class = [
        {
            **a,
            "kind_signal": w.get("kind_signal", ""),
            "origin": w.get("origin", ""),
            "source_document_id": w.get("source_document_id"),
            "source_chunk_id": w.get("source_chunk_id"),
            "evidence_refs": [
                {k: r.get(k) for k in ("document_id", "chunk_id", "section_path", "quote")}
                for r in (w.get("evidence_refs") or [])
                if isinstance(r, dict)
            ],
        }
        for a, w in zip(actual, report.get("weaknesses", []))
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
        "signal_share": None,
        "dup_per_golden": None,
        "judge_fn": None,
        "classification_json": None,
        "exec_coverage": None,
        "exec_faithfulness": None,
        "exec_violation": None,
        "exec_overall": None,
    }
    if judge_mode != "full":
        return metrics, match_rows, judge_records

    # Signal/noise classification of every reported weakness against the chunks
    if actual:
        sel_chunks, scope = select_chunks_for_classification(
            report, chunks or [], settings.JUDGE_CLASSIFY_CHUNK_BUDGET_CHARS
        )
        class_out, recs = judge.classify_findings(
            expected=[{**e, "optional": expected_by_id[e["id"]]["optional"]} for e in expected],
            actual=actual_for_class,
            match_out=match_out.model_dump(),
            chunks=sel_chunks,
            chunk_scope=scope,
        )
        judge_records.extend(recs)
        cls_rows = [c.model_dump() for c in class_out.classification]
        cls = score_classification(cls_rows)
        metrics.update(
            signal_share=cls.signal_share,
            dup_per_golden=cls.dup_per_golden,
            judge_fn=cls.judge_fn,
            classification_json=json.dumps(
                {
                    "chunk_scope": scope,
                    "n_chunks_sent": len(sel_chunks),
                    "counts": cls.counts,
                    "judge_fp_match": cls.judge_fp_match,
                    "classification": cls_rows,
                    "missed_goldens": [m.model_dump() for m in class_out.missed_goldens],
                }
            ),
        )

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


def _summary_line(cr: CaseResult) -> str:
    def f(v, fmt="{:.2f}"):
        return "—" if v is None else fmt.format(v)

    return (
        f"band={cr.aggregate_band or '—'} band_err={f(cr.band_error, '{:+d}')} "
        f"n={cr.n_weaknesses} tp/fp/fn={cr.tp}/{cr.fp}/{cr.fn} "
        f"recall={f(cr.recall)} signal={f(cr.signal_share)} "
        f"dup/golden={f(cr.dup_per_golden)} judge_fn={cr.judge_fn} "
        f"exec={f(cr.exec_overall, '{:.0f}')}"
    )


def _grade_and_persist(session, cr: CaseResult, client: AppClient, case: Case, report: dict, config: RunConfig) -> str:
    """Token collection + judge grading for a report already on `cr`.
    Returns the case status (ok | judge_error). Shared by run_one and grade_stored."""
    tokens = collect.collect_model_calls(cr.assessment_id)
    if tokens:
        cr.tokens_json = json.dumps(tokens)

    if config.judge_mode == "none":
        print(f"[case {case.id} rep {cr.repetition}] {_summary_line(cr)}")
        return "ok"

    try:
        chunks: list[dict] = []
        if config.judge_mode == "full":
            for doc in report.get("documents", []):
                chunks.extend(client.get_chunks(doc["id"]))
        judge = Judge(model=config.judge_model)
        try:
            metrics, match_rows, judge_records = grade_case(
                judge, case, report, config.match_confidence_threshold,
                judge_mode=config.judge_mode, chunks=chunks,
            )
        finally:
            judge.close()
        for k, v in metrics.items():
            setattr(cr, k, v)
        for row in match_rows:
            session.add(FindingMatch(case_result_id=cr.id, **row))
        _persist_judge_records(session, cr.id, judge_records)
        print(f"[case {case.id} rep {cr.repetition}] {_summary_line(cr)}")
        return "ok"
    except JudgeError as e:
        cr.error_stage = "judge"
        cr.error_detail = str(e)
        _persist_judge_records(session, cr.id, e.records)
        return "judge_error"


def _new_run(client: AppClient, config: RunConfig, backend_url: str) -> int:
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
                "finding_class": prompts.FINDING_CLASS_VERSION,
                "exec_rubric": prompts.EXEC_RUBRIC_VERSION,
            }
        ),
        config_json=config.to_json(),
        notes=config.notes,
    )
    session.add(run)
    session.commit()
    run_id = run.id
    session.close()
    return run_id


def _finish_run(run_id: int, statuses: list[str]) -> str:
    ok = sum(1 for s in statuses if s == "ok")
    run_status = "done" if ok == len(statuses) else ("failed" if ok == 0 else "partial")
    session = get_session()
    run = session.get(Run, run_id)
    run.status = run_status
    run.finished_at = utcnow()
    session.add(run)
    session.commit()
    session.close()
    return run_status


def grade_stored(case: Case, assessment_id: int, config: RunConfig) -> tuple[int, str]:
    """Grade an assessment that already exists on the backend — no pipeline
    stages, no app LLM cost. Records a run (mode=grade) with one case_result
    whose timings are empty; tokens_json reflects the stored assessment's
    original pipeline cost. Used to iterate on a single re-run stage
    (TESTING.md §2) and to grade the baseline canary without re-running it."""
    backend_url = settings.BENCH_BACKEND_URL
    client = AppClient(backend_url)
    if not client.health():
        client.close()
        raise SystemExit(f"backend at {backend_url} is not healthy — start it first")
    config.mode = "grade"
    run_id = _new_run(client, config, backend_url)

    session = get_session()
    cr = CaseResult(run_id=run_id, case_id=case.id, repetition=1, assessment_id=assessment_id)
    session.add(cr)
    session.commit()
    status = "ok"
    try:
        try:
            report = client.get_report(assessment_id)
        except Exception as e:
            cr.error_stage = "report"
            cr.error_detail = str(e)
            raise
        cr.timings_json = "{}"
        cr.report_json = json.dumps(report)
        agg = report.get("aggregate", {})
        cr.aggregate_band = agg.get("band", "")
        cr.aggregate_rank = agg.get("rank")
        for k, v in deterministic_metrics(case, report).items():
            setattr(cr, k, v)
        status = _grade_and_persist(session, cr, client, case, report, config)
    except Exception as e:
        status = "error"
        cr.error_stage = cr.error_stage or "unexpected"
        cr.error_detail = cr.error_detail or repr(e)
    finally:
        cr.status = status
        cr.finished_at = utcnow()
        session.commit()
        session.close()
        client.close()
    print(f"[case {case.id} grade of assessment {assessment_id}] {status}")
    return run_id, _finish_run(run_id, [status])


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
                client, case, config.model_overrides, config.skip_narratives
            )
            cr.assessment_id = assessment_id
            cr.timings_json = json.dumps(timings)
            cr.report_json = json.dumps(report)
            agg = report.get("aggregate", {})
            cr.aggregate_band = agg.get("band", "")
            cr.aggregate_rank = agg.get("rank")
            for k, v in deterministic_metrics(case, report).items():
                setattr(cr, k, v)
        except StageError as e:
            status = "error"
            cr.error_stage = e.stage
            cr.error_detail = e.detail
            assessment_id = e.assessment_id
            cr.assessment_id = assessment_id  # keep the link for debugging / bench grade
            raise

        status = _grade_and_persist(session, cr, client, case, report, config)
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
    if client.dev_cache_active() and not config.allow_dev_cache:
        client.close()
        raise SystemExit(
            f"backend at {backend_url} has LLM_DEV_CACHE on — it would serve cached "
            "model responses. Restart it without the cache, or pass --allow-dev-cache "
            "for a plumbing-only run (never for a measurement)."
        )

    run_id = _new_run(client, config, backend_url)
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

    return run_id, _finish_run(run_id, statuses)
