"""Judge prompt templates. Bump PROMPT_VERSION on any wording change —
versions are recorded per run so scores stay attributable."""

from __future__ import annotations

import json

WEAKNESS_MATCH_VERSION = "wm-1"
FINDING_CLASS_VERSION = "fc-2"
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


# ---- signal/noise classification (categories from testdata/_results/cases/fp_spec.md) ----

FINDING_CLASS_CATEGORIES = (
    "TP", "TP_OPTIONAL", "DUP_OF_TP", "LEGIT_UNKEYED", "BOILERPLATE", "MISREAD",
    "JUDGE_FN", "JUDGE_FP_MATCH",
)

FINDING_CLASS_SYSTEM = """\
You are auditing the weaknesses reported by an automated cyber third-party \
risk assessment tool. You receive the hand-authored answer key (golden \
weaknesses, some marked optional), the tool's reported weaknesses, a prior \
matching of goldens to reported weaknesses, and the parsed text of the \
evidence documents (chunks). The chunks are the ONLY source of truth about \
what the documents say — verify every classification against them; never \
trust a reported description alone.

Classify EVERY reported weakness into exactly one category:
- TP — the prior matching linked it to a required golden and, on your \
reading of the chunks, it really is that finding.
- TP_OPTIONAL — linked to an optional golden and really is that finding.
- DUP_OF_TP — a second/third statement of a deficiency already counted as \
TP/TP_OPTIONAL (same underlying deficiency: e.g. the same contradiction \
reported under another control, the policy-gap restatement of a \
questionnaire "no", the auditor's opinion paragraph naming an exception \
already reported). Name the golden.
- LEGIT_UNKEYED — a genuine, evidence-backed deficiency of the vendor's \
control environment that the answer key simply does not list (a reasonable \
analyst would report it). Be strict: it must be grounded in a chunk, be a \
real deficiency, and survive a check of the WHOLE bundle — before choosing \
this category search every other document for the element claimed absent. \
"Document A does not cover Y" is not a deficiency when Y is covered by \
document B in the bundle (e.g. the InfoSec policy lacking BC/DR or \
sub-processor content when separate BC/DR and TPRM policies are supplied; \
a policy pointing to a separate incident-response plan for classification).
- BOILERPLATE — technically true but not a vendor weakness: complementary \
user-entity control (CUEC) listings, "policy does not name an owner / review \
cadence" style items where the element is present, absences in one document \
that another supplied document covers, restatements that a separate document \
exists, disclosed design choices that breach no requirement stated anywhere \
in the bundle or the answer key (e.g. proportionate lighter diligence for \
low-tier sub-processors, an archive tier explicitly excluded from RTO), \
self-asserted maturity notes, scope statements, generic recommendations, \
restatements of facts already reported by another weakness that add no new \
deficiency.
- MISREAD — the description misrepresents the evidence: says the vendor \
"did not confirm" when the comment in the same chunk clearly confirms; treats \
"See comment" as a negative answer when the comment is affirmative; claims an \
absence when the text contains it; wrong numbers or dates; a "contradiction" \
that is not one; staleness computed from the wrong date.
- JUDGE_FN — the prior matching left it unmatched, but it does in fact \
express a golden finding. Name the golden.
- JUDGE_FP_MATCH — the prior matching linked it to a golden but it does NOT \
really express that golden. Name the golden and say why.

Rules:
- Every reported weakness id appears exactly once. Use the integer ids given.
- "golden" is required for TP, TP_OPTIONAL, DUP_OF_TP, JUDGE_FN and \
JUDGE_FP_MATCH and must be null otherwise.
- At most ONE reported weakness may be TP (or TP_OPTIONAL / JUDGE_FN) per \
golden; further statements of the same golden are DUP_OF_TP.
- Every entry needs a one-line reason that cites the decisive chunk \
(document + section) or explains why nothing in the chunks supports it.
- For every golden the prior matching marked missed, say whether any chunk \
contains the underlying fact (ingestion problem vs reasoning problem).

Respond with ONLY a JSON object, no markdown fences, matching:
{
  "classification": [{"id": <int>, "category": "<one of the categories>",
                      "golden": "<golden id or null>", "reason": "<one line>"}],
  "missed_goldens": [{"golden": "<golden id>", "fact_in_chunks": true|false,
                      "where": "<document / section or empty>", "note": "<one line>"}]
}
"""


def finding_class_user(
    expected: list[dict],
    actual: list[dict],
    match_out: dict,
    chunks: list[dict],
    chunk_scope: str,
) -> str:
    scope_note = (
        "all chunks of every evidence document"
        if chunk_scope == "full"
        else "only the chunks cited by the reported weaknesses (bundle too large "
        "to send whole — treat uncited facts as unverifiable, not absent)"
    )
    return (
        "# Expected (golden) weaknesses — the answer key\n"
        + json.dumps(expected, indent=2)
        + "\n\n# Weaknesses reported by the tool under test\n"
        + json.dumps(actual, indent=2)
        + "\n\n# Prior matching (golden ↔ reported)\n"
        + json.dumps(match_out, indent=2)
        + f"\n\n# Evidence chunks ({scope_note})\n"
        + json.dumps(chunks, indent=1, ensure_ascii=False)
        + "\n\nClassify every reported weakness now. JSON only."
    )
