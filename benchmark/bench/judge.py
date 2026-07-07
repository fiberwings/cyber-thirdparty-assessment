"""LLM judge: direct OpenRouter calls, independent of the main app.

Every invocation is persisted verbatim (request messages + raw response) by
the runner so grades are auditable. The judge emits raw match/rubric
structures only; numeric aggregation lives in metrics.py.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Literal, TypeVar

import httpx
from pydantic import BaseModel, Field, ValidationError

from . import prompts
from .config import settings


class JudgeError(Exception):
    """Judge failed after retry. Carries the per-attempt call records so the
    runner can still persist them for auditability."""

    def __init__(self, message: str, records: list["JudgeCallRecord"] | None = None):
        super().__init__(message)
        self.records = records or []


# ---- output schemas ----

Confidence = Literal["high", "medium", "low", "unknown"]


class MatchEntry(BaseModel):
    expected_id: str
    actual_id: int
    confidence: Confidence
    justification: str


class UnmatchedExpected(BaseModel):
    expected_id: str
    justification: str


class UnmatchedActual(BaseModel):
    actual_id: int
    justification: str


class WeaknessMatchOut(BaseModel):
    matches: list[MatchEntry] = Field(default_factory=list)
    unmatched_expected: list[UnmatchedExpected] = Field(default_factory=list)
    unmatched_actual: list[UnmatchedActual] = Field(default_factory=list)


class CoverageItem(BaseModel):
    point_id: str
    status: Literal["covered", "partial", "missing", "unknown"]
    justification: str


class ViolationItem(BaseModel):
    claim_id: str
    status: Literal["violated", "clean", "unknown"]
    justification: str


class Faithfulness(BaseModel):
    score: int = Field(ge=0, le=5)
    unsupported_claims: list[str] = Field(default_factory=list)
    justification: str


class ExecRubricOut(BaseModel):
    coverage: list[CoverageItem] = Field(default_factory=list)
    violations: list[ViolationItem] = Field(default_factory=list)
    faithfulness: Faithfulness


@dataclass
class JudgeCallRecord:
    """Everything the runner persists to the judge_call table."""

    purpose: str
    model_id: str
    prompt_version: str
    request_json: str
    response_json: str
    latency_ms: int
    input_tokens: int | None
    output_tokens: int | None
    ok: bool
    error: str = ""


T = TypeVar("T", bound=BaseModel)


def _strip_fences(text: str) -> str:
    text = text.strip()
    m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    return m.group(1) if m else text


class Judge:
    def __init__(self, model: str | None = None, api_key: str | None = None):
        self.model = model or settings.JUDGE_MODEL
        key = api_key or settings.OPENROUTER_API_KEY
        if not key:
            raise JudgeError("OPENROUTER_API_KEY is not set")
        self.http = httpx.Client(
            base_url=settings.OPENROUTER_BASE_URL.rstrip("/"),
            headers={"Authorization": f"Bearer {key}"},
            timeout=300.0,
        )

    def close(self) -> None:
        self.http.close()

    # ---- public API ----

    def match_weaknesses(
        self, expected: list[dict], actual: list[dict]
    ) -> tuple[WeaknessMatchOut, list[JudgeCallRecord]]:
        def structural_check(out: WeaknessMatchOut) -> str | None:
            exp_ids = {e["id"] for e in expected}
            act_ids = {a["id"] for a in actual}
            seen_exp = [m.expected_id for m in out.matches] + [
                u.expected_id for u in out.unmatched_expected
            ]
            seen_act = [m.actual_id for m in out.matches] + [
                u.actual_id for u in out.unmatched_actual
            ]
            problems = []
            if sorted(seen_exp) != sorted(exp_ids):
                problems.append(
                    f"expected ids mismatch: answer key has {sorted(exp_ids)}, "
                    f"you produced {sorted(seen_exp)} (each exactly once)"
                )
            if sorted(seen_act) != sorted(act_ids):
                problems.append(
                    f"actual ids mismatch: tool output has {sorted(act_ids)}, "
                    f"you produced {sorted(seen_act)} (each exactly once)"
                )
            return "; ".join(problems) or None

        return self._call(
            purpose="weakness_match",
            prompt_version=prompts.WEAKNESS_MATCH_VERSION,
            system=prompts.WEAKNESS_MATCH_SYSTEM,
            user=prompts.weakness_match_user(expected, actual),
            schema=WeaknessMatchOut,
            structural_check=structural_check,
        )

    def grade_exec_summary(
        self,
        summary_text: str,
        evidence_digest: str,
        must_cover: list[dict],
        must_not_claim: list[dict],
    ) -> tuple[ExecRubricOut, list[JudgeCallRecord]]:
        def structural_check(out: ExecRubricOut) -> str | None:
            want_pts = sorted(p["id"] for p in must_cover)
            got_pts = sorted(c.point_id for c in out.coverage)
            want_cl = sorted(c["id"] for c in must_not_claim)
            got_cl = sorted(v.claim_id for v in out.violations)
            problems = []
            if want_pts != got_pts:
                problems.append(f"coverage ids: want {want_pts}, got {got_pts}")
            if want_cl != got_cl:
                problems.append(f"violation ids: want {want_cl}, got {got_cl}")
            return "; ".join(problems) or None

        return self._call(
            purpose="exec_rubric",
            prompt_version=prompts.EXEC_RUBRIC_VERSION,
            system=prompts.EXEC_RUBRIC_SYSTEM,
            user=prompts.exec_rubric_user(
                summary_text, evidence_digest, must_cover, must_not_claim
            ),
            schema=ExecRubricOut,
            structural_check=structural_check,
        )

    # ---- internals ----

    def _call(
        self,
        purpose: str,
        prompt_version: str,
        system: str,
        user: str,
        schema: type[T],
        structural_check,
    ) -> tuple[T, list[JudgeCallRecord]]:
        """One judge invocation with a single validation-driven retry.

        Returns (validated output, call records — one per attempt) so failed
        first attempts are persisted too.
        """
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        records: list[JudgeCallRecord] = []
        last_error = ""

        for attempt in range(2):
            body = {
                "model": self.model,
                "messages": messages,
                "temperature": 0,
                "max_tokens": settings.JUDGE_MAX_TOKENS,
            }
            t0 = time.monotonic()
            error = ""
            raw_text = ""
            usage: dict = {}
            try:
                r = self.http.post("/chat/completions", json=body)
                r.raise_for_status()
                data = r.json()
                raw_text = data["choices"][0]["message"]["content"] or ""
                usage = data.get("usage") or {}
            except httpx.HTTPError as e:
                error = f"transport: {e}"
            latency_ms = int((time.monotonic() - t0) * 1000)

            parsed: T | None = None
            if not error:
                try:
                    parsed = schema.model_validate(json.loads(_strip_fences(raw_text)))
                except (json.JSONDecodeError, ValidationError) as e:
                    error = f"parse/validation: {e}"
                else:
                    structural = structural_check(parsed)
                    if structural:
                        error = f"structural: {structural}"
                        parsed = None

            records.append(
                JudgeCallRecord(
                    purpose=purpose,
                    model_id=self.model,
                    prompt_version=prompt_version,
                    request_json=json.dumps(body),
                    response_json=raw_text,
                    latency_ms=latency_ms,
                    input_tokens=usage.get("prompt_tokens"),
                    output_tokens=usage.get("completion_tokens"),
                    ok=parsed is not None,
                    error=error,
                )
            )
            if parsed is not None:
                return parsed, records

            last_error = error
            # retry once with the failure appended so the judge can self-correct
            messages = messages + [
                {"role": "assistant", "content": raw_text or "(no output)"},
                {
                    "role": "user",
                    "content": (
                        f"Your previous answer was rejected: {error}. "
                        "Respond again with ONLY the corrected JSON object."
                    ),
                },
            ]

        raise JudgeError(f"{purpose} failed after retry: {last_error}", records)
