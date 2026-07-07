"""Judge prompt templates. Bump PROMPT_VERSION on any wording change —
versions are recorded per run so scores stay attributable."""

from __future__ import annotations

import json

WEAKNESS_MATCH_VERSION = "wm-1"
EXEC_RUBRIC_VERSION = "es-1"

WEAKNESS_MATCH_SYSTEM = """\
You are grading an automated cyber third-party risk assessment against a \
hand-authored answer key. You compare the weaknesses the tool reported with \
the expected (golden) weaknesses and decide which are semantically the same \
finding.

Rules:
- A match requires the SAME underlying deficiency, not merely the same topic \
area. "MFA not enforced for admins" matches "administrative access lacks \
multi-factor authentication", but NOT "password policy is weak".
- Each expected weakness matches AT MOST ONE actual weakness (the best one; \
mention plausible secondary candidates in the justification).
- Each actual weakness appears in at most one match.
- If you are genuinely uncertain whether two items describe the same \
deficiency, use confidence "unknown" rather than guessing.
- Every entry MUST include a justification quoting the decisive phrase from \
BOTH the expected and the actual text (or explaining why nothing matches).
- Every expected id and every actual id must appear exactly once across \
matches, unmatched_expected and unmatched_actual.

Respond with ONLY a JSON object, no markdown fences, matching:
{
  "matches": [{"expected_id": "<golden id>", "actual_id": <int>,
               "confidence": "high"|"medium"|"low"|"unknown",
               "justification": "<quote both sides>"}],
  "unmatched_expected": [{"expected_id": "<golden id>", "justification": "..."}],
  "unmatched_actual": [{"actual_id": <int>, "justification": "..."}]
}
"""


def weakness_match_user(expected: list[dict], actual: list[dict]) -> str:
    return (
        "# Expected (golden) weaknesses — the answer key\n"
        + json.dumps(expected, indent=2)
        + "\n\n# Weaknesses reported by the tool under test\n"
        + json.dumps(actual, indent=2)
        + "\n\nMatch them now. JSON only."
    )


EXEC_RUBRIC_SYSTEM = """\
You are grading the executive summary of an automated cyber third-party risk \
assessment against a rubric. Judge ONLY from the materials provided — do not \
use outside knowledge about the vendor or industry.

You evaluate three things:
1. coverage: for each must-cover point, is it substantively addressed in the \
summary ("covered"), mentioned without substance ("partial"), absent \
("missing"), or undecidable from the materials ("unknown")?
2. violations: for each forbidden claim, does the summary assert it \
("violated") or not ("clean")? A hedged, negated, or caveated mention is NOT \
a violation. Use "unknown" only when the summary is genuinely ambiguous.
3. faithfulness: 0-5, how faithful the summary is to the evidence digest (5 = \
every claim traceable to the digest; 0 = substantially fabricated). List any \
summary claims not supported by the digest in unsupported_claims.

Every item MUST include a justification citing the relevant summary wording.

Respond with ONLY a JSON object, no markdown fences, matching:
{
  "coverage": [{"point_id": "<id>", "status": "covered"|"partial"|"missing"|"unknown",
                "justification": "..."}],
  "violations": [{"claim_id": "<id>", "status": "violated"|"clean"|"unknown",
                  "justification": "..."}],
  "faithfulness": {"score": <0-5 int>, "unsupported_claims": ["..."],
                   "justification": "..."}
}
"""


def exec_rubric_user(
    summary_text: str,
    evidence_digest: str,
    must_cover: list[dict],
    must_not_claim: list[dict],
) -> str:
    return (
        "# Executive summary under evaluation\n"
        + summary_text
        + "\n\n# Evidence digest (what the assessment actually found — the "
        "ground truth for faithfulness)\n"
        + evidence_digest
        + "\n\n# Must-cover points\n"
        + json.dumps(must_cover, indent=2)
        + "\n\n# Forbidden claims (must NOT be asserted)\n"
        + json.dumps(must_not_claim, indent=2)
        + "\n\nGrade it now. JSON only."
    )
