"""Typed HTTP client for the TPRM app under test.

This is the ONLY integration point with the main app: plain HTTP against a
running backend. Contracts mirrored from backend/app/schemas/api.py (ReportOut,
TaskStatusRead, ModelOverrides) — kept as loose dicts on purpose so benign
additive changes in the app don't break the harness.
"""

from __future__ import annotations

import mimetypes
import time
from pathlib import Path
from typing import Any

import httpx

from .config import settings


class StageError(Exception):
    """A pipeline stage failed or timed out for one case. Carries the
    assessment id (when one was created) so the case row keeps pointing at
    the assessment left on the backend."""

    def __init__(
        self,
        stage: str,
        detail: str,
        assessment_id: int | None = None,
        *,
        gap_failed_controls: int | None = None,
    ):
        self.stage = stage
        self.detail = detail
        self.assessment_id = assessment_id
        # Set when the gap-analysis phase completed with controls whose AI
        # run failed (backend `failed_targets`): the assessment is
        # incomplete, not broken, but its metrics would be an artefact.
        self.gap_failed_controls = gap_failed_controls
        super().__init__(f"[{stage}] {detail}")


# Maps runner stage names to the durable phase keys in AssessmentRead.phases
# (six UI phases: scoping, scenarios, evidence, correlation, analysis, score —
# see backend serializers.compute_phase_status). Used as a fallback when the
# in-memory task registry loses a task (404).
STAGE_TO_PHASE = {
    "scenarios": "scenarios",
    "extraction": "evidence",
    "cross_correlate": "correlation",
    "gap_analysis": "analysis",
    "narratives": "score",
}


class AppClient:
    def __init__(self, base_url: str | None = None, timeout: float = 120.0):
        self.base_url = (base_url or settings.BENCH_BACKEND_URL).rstrip("/")
        self.http = httpx.Client(base_url=self.base_url, timeout=timeout)

    def close(self) -> None:
        self.http.close()

    # ---- basics ----

    def health(self) -> bool:
        try:
            r = self.http.get("/api/health")
            return r.status_code == 200 and r.json().get("ok") is True
        except httpx.HTTPError:
            return False

    def get_models(self) -> list[dict]:
        r = self.http.get("/api/models")
        r.raise_for_status()
        return r.json()

    # ---- assessment lifecycle ----

    def create_assessment(self, vendor_name: str) -> int:
        r = self.http.post("/api/assessments", json={"vendor_name": vendor_name})
        r.raise_for_status()
        return r.json()["id"]

    def delete_assessment(self, assessment_id: int) -> None:
        r = self.http.delete(f"/api/assessments/{assessment_id}")
        r.raise_for_status()

    def get_assessment(self, assessment_id: int) -> dict:
        r = self.http.get(f"/api/assessments/{assessment_id}")
        r.raise_for_status()
        return r.json()

    def gap_analysis_failed_targets(self, assessment_id: int) -> list[str]:
        """Controls the gap-analysis phase finished *without* assessing
        (`ControlAssessment.last_error` set — e.g. a 429 storm exhausted
        the router's retries). The backend marks the phase done with a
        warning so a human can resume; a benchmark must not score the
        partial result as if it were complete."""
        phases = self.get_assessment(assessment_id).get("phases") or {}
        analysis = phases.get(STAGE_TO_PHASE["gap_analysis"]) or {}
        return list(analysis.get("failed_targets") or [])

    def set_model_overrides(self, assessment_id: int, overrides: dict[str, str]) -> None:
        r = self.http.patch(
            f"/api/assessments/{assessment_id}/model-overrides", json=overrides
        )
        r.raise_for_status()

    def set_settings(
        self, assessment_id: int, as_of_date: str | None, standards_profile: dict | None
    ) -> None:
        body: dict = {}
        if as_of_date:
            body["as_of_date"] = as_of_date
        if standards_profile:
            body["standards_profile"] = standards_profile
        if not body:
            return
        r = self.http.patch(f"/api/assessments/{assessment_id}/settings", json=body)
        r.raise_for_status()

    def dev_cache_active(self) -> bool:
        """True when the backend would serve cached model responses (dev-only
        switch). A real benchmark run must refuse to measure against it."""
        try:
            r = self.http.get("/api/health")
            return bool(r.json().get("llm_dev_cache")) if r.status_code == 200 else False
        except (httpx.HTTPError, ValueError):
            return False

    def set_description(self, assessment_id: int, text: str) -> None:
        r = self.http.post(
            f"/api/assessments/{assessment_id}/description", json={"text": text}
        )
        r.raise_for_status()

    def force_continue_scoping(self, assessment_id: int) -> None:
        r = self.http.post(f"/api/assessments/{assessment_id}/scoping/force-continue")
        r.raise_for_status()

    # ---- pipeline stages ----

    def generate_scenarios(self, assessment_id: int) -> str:
        r = self.http.post(f"/api/assessments/{assessment_id}/scenarios/generate")
        r.raise_for_status()
        return r.json()["task_id"]

    def upload_document(self, assessment_id: int, path: Path, kind: str) -> dict:
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        with path.open("rb") as f:
            r = self.http.post(
                f"/api/assessments/{assessment_id}/documents",
                data={"kind": kind},
                files={"file": (path.name, f, mime)},
                timeout=300.0,  # synchronous parse of large PDFs
            )
        r.raise_for_status()
        return r.json()

    def cross_correlate(self, assessment_id: int) -> str:
        r = self.http.post(f"/api/assessments/{assessment_id}/cross-correlate")
        r.raise_for_status()
        return r.json()["task_id"]

    def run_gap_analysis(self, assessment_id: int) -> str:
        r = self.http.post(f"/api/assessments/{assessment_id}/gap-analysis/run")
        r.raise_for_status()
        return r.json()["task_id"]

    def recalculate(self, assessment_id: int) -> dict:
        r = self.http.post(f"/api/assessments/{assessment_id}/recalculate")
        r.raise_for_status()
        return r.json()

    def run_narratives(self, assessment_id: int) -> str:
        r = self.http.post(f"/api/assessments/{assessment_id}/narratives/run")
        r.raise_for_status()
        return r.json()["task_id"]

    def get_report(self, assessment_id: int) -> dict:
        r = self.http.get(f"/api/assessments/{assessment_id}/report")
        r.raise_for_status()
        return r.json()

    def get_chunks(self, document_id: int) -> list[dict]:
        r = self.http.get(f"/api/documents/{document_id}/chunks")
        r.raise_for_status()
        return r.json()

    # ---- task polling ----

    def wait_task(
        self,
        task_id: str,
        stage: str,
        assessment_id: int | None = None,
        *,
        idle_timeout_s: float | None = None,
        max_stage_s: float | None = None,
        poll_interval: float | None = None,
    ) -> dict[str, Any]:
        """Poll a background task until done/error, waiting on *liveness*.

        The idle clock restarts whenever the task shows activity — the
        backend's `last_activity_at` (advanced by every streamed token and
        keepalive), or, against an older backend, a change of progress /
        detail. A stage therefore fails only after IDLE_TIMEOUT_S without any
        activity, or past the MAX_STAGE_S runaway ceiling; a slow model that
        keeps producing tokens is never cut off.

        On a 404 (in-memory registry lost, e.g. backend restart) fall back to
        the durable per-phase state on the assessment.
        """
        interval = poll_interval or settings.POLL_INTERVAL
        idle_limit = idle_timeout_s if idle_timeout_s is not None else settings.IDLE_TIMEOUT_S
        ceiling = max_stage_s if max_stage_s is not None else settings.MAX_STAGE_S
        t0 = time.monotonic()
        last_change = t0
        fingerprint: tuple | None = None
        while True:
            r = self.http.get(f"/api/tasks/{task_id}")
            if r.status_code == 404:
                return self._phase_fallback(stage, assessment_id)
            r.raise_for_status()
            status = r.json()
            if status["status"] == "done":
                return status
            if status["status"] == "error":
                raise StageError(stage, status.get("detail") or status.get("error") or "task errored")
            now = time.monotonic()
            current = (status.get("last_activity_at"), status.get("progress"), status.get("detail"))
            if current != fingerprint:
                fingerprint = current
                last_change = now
            elif now - last_change > idle_limit:
                raise StageError(
                    stage,
                    f"timed out: no activity for {now - last_change:.0f}s "
                    f"(IDLE_TIMEOUT_S={idle_limit:.0f}, task {task_id}, "
                    f"last detail {status.get('detail')!r})",
                )
            if now - t0 > ceiling:
                raise StageError(
                    stage, f"timed out after {now - t0:.0f}s (MAX_STAGE_S={ceiling:.0f}) (task {task_id})"
                )
            time.sleep(interval)

    def _phase_fallback(self, stage: str, assessment_id: int | None) -> dict[str, Any]:
        if assessment_id is None:
            raise StageError(stage, "task lost (404) and no assessment_id for fallback")
        phase_key = STAGE_TO_PHASE.get(stage)
        phases = self.get_assessment(assessment_id).get("phases") or {}
        phase = (phases.get(phase_key) or {}) if phase_key else {}
        if phase.get("state") == "done":
            return {"status": "done", "progress": 1.0, "detail": "recovered via phases"}
        raise StageError(
            stage,
            f"task lost (404); durable phase {phase_key!r} is "
            f"{phase.get('state')!r}: {phase.get('error')!r}",
        )
