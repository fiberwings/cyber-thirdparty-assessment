# Phase 5 — Scoring & severity (R5, R6)

Started 2026-08-31 after Phase 4 (`2e5568a`). LLM-free: engine rewritten, iterated via `/recalculate` on a
`.backup` copy of the stored DB (stored assessments untouched pending user review — floor).

## Design (engine.py rewrite)
- **Weaknesses change control state, not arithmetic**: the worst mapped severity per control forces effectiveness →
  `weak` (high/critical, an operating exception) or caps it at `adequate` (medium); low changes nothing. Auditable via
  `state_downgrades` ("CODE: strong→weak (high)"). The additive per-row uplift (which saturated at cap in 65/72 baseline
  scenarios) is gone.
- **Bounded, deficiency-gated uplift**: +1 likelihood band only for ≥ 1 critical or ≥ 2 distinct high/critical
  deficiencies mapped to the scenario; never more.
- **Residual ≤ inherent** unless ≥ 1 of those deficiencies is auditor-tested (`evidence_strength = auditor_tested`),
  then at most inherent + 1. Evidence strength comes from the confirmation pass (new `severity` +
  `evidence_strength` decision fields — severity re-assessed by consequence) with a deterministic `kind_signal`
  fallback for legacy rows (soc/pentest/iso/attestation → auditor_tested; questionnaire/cross-doc → vendor_admitted;
  policy/dpa absence → inferred_absence).
- **Meta-issues become a reported confidence band** (high/medium/low per scenario; aggregate = worst) — never a score
  input. Exec summary is told to qualify wording accordingly; UI shows it.
- **Aggregation**: the impact-weighted mean drives the band (the old "max(top-2, weighted)" let one outlier scenario set
  the vendor band); VeryHigh requires the weighted mean to round there or ≥ 2 independent VeryHigh scenarios.

Found along the way: `Optional` missing from `schemas/ai.py` imports (lazy pydantic failure that the confirmation
keep-and-flag path masked — caught by tests), and rescoring a WAL-mode SQLite via `cp` yields a stale snapshot (use
`.backup`; first table had wrong "old" bands for the newest assessment).

## Band-change table (rescored COPY; the review artefact for the floor)
| id | vendor | old | new | expected | Δ | confidence |
|---|---|---|---|---|---|---|
| 6 | cloudnimbus | VeryHigh | **Moderate** | Moderate | 0 | low |
| 7 | globaltalent | VeryHigh | **VeryHigh** | VeryHigh | 0 | low |
| 8 | meridian | VeryHigh | High | Moderate | +1 | medium |
| 9 | orbitclear (baseline data) | VeryHigh | VeryHigh | High | +1 | low |
| 10 | quantedge | VeryHigh | High | Moderate | +1 | low |
| 11 | verifypro | VeryHigh | VeryHigh | High | +1 | low |
| 14 | orbitclear (Phase 2 canary) | VeryHigh | **High** | High | 0 | low |
| 17 | orbitclear (Phase 4 canary) | VeryHigh | Moderate | High | −1 | low |

Per-scenario changes: 60+ listed by the rescore script (`scratchpad/rescore_table.py` output, reproduced in chat for
review). Residual > inherent in exactly 3 scenarios, each gated by auditor-tested high/critical evidence
(globaltalent PAYROLL_FRAUD_SFTP; verifypro DEEPFAKE_KYC_BYPASS, INSIDER_DATA_EXFILTRATION_MAURITIUS); **0 floor
violations**.

Reading: discrimination is restored (all-VeryHigh → Moderate…VeryHigh spread). The four old assessments carry
pre-Phase-3 weakness sets (40–120 rows, never confirmation-reviewed), which biases them +1; the clean-pipeline
canary a14 lands exactly on the expected High, and a17 (whose review dropped/merged more) at Moderate (−1).

## Gate
- Floor "no residual > inherent without audit/pen-test-evidenced failure" — **met** (3 gated cases, 0 violations).
- Floor "every band change listed for the user / user reviews before merge" — table above; **user review pending**.
- Target "band within one level of expected ≥ 5/6" — **met, 6/6** (and 8/8 incl. canaries).

## Review outcome (2026-08-31, user)
The user examined the one underrating (a17 Moderate vs expected High). Diagnosis: six scenarios carry exactly one
high — the auditor-tested SOC AC-04 MFA exception — which forces the control state to weak but does not meet the
uplift quorum (≥ 1 critical or ≥ 2 highs); confirmation had re-assessed G3/G4 to medium by consequence; weighted mean
2.47 sat just under the boundary. A candidate "rule B" (a single auditor-tested high also satisfies the uplift gate)
was simulated on all eight assessments: it fixes both clean canaries exactly (a14, a17 → High) but pushes cloudnimbus
to +2 on its pre-Phase-3 noisy weakness set (14 known misreads).

**Decision: keep the current gate for now (option a)** — a17's −1 accepted; rule B re-examined at Phase 6, where all
six vendors re-run through the current pipeline (if underrating with auditor-tested evidence persists on reviewed
data, adopt rule B then). Separately, aggregate rounding changed from Python's banker's rounding to half-up
(2.5 → High) — a boundary-direction bug; changes no current band, unit-tested.

## Closed
2026-08-31 — committed after user review.
