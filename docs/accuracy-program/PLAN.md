# Accuracy improvement program — PLAN (frozen, rev. 2 of 2026-08-30)

Source: `testdata/_results/03_improvement_recommendations.md` (evaluation of 2026-08-29, app @ d391c80) **as revised on
2026-08-30** — see the revision note at the top of that document. Where the two disagree, this plan is authoritative.
This file is written once. Progress goes in `STATE.md`; per-phase detail in `phases/`. Do not edit gates mid-phase.

## Design stance
- **Prefer judgement over scaffolding.** LLMs keep improving; the app should get better for free. Each change is either
  *structure the product needs regardless of model* (inputs, scoring, robustness, evidence schema) or *a whole-context
  judgement call that must cite quotes*. Avoid hand-built stages that substitute for model reasoning (rule lists, entity
  ledgers, heuristic dedupe) unless the benchmark shows the model still cannot do it with the whole bundle in context.
- **Measure against evidence, not only against golden lists.** The signal/noise classification (TP / duplicate / legit
  unkeyed / boilerplate / misread) is a first-class metric; it is what shows whether a model or code change actually helped.
- **Scaffolds are provisional.** Any compensating stage (confirmation rules, dedupe merge, attestation profile) carries a
  note in its phase file: "re-test with this disabled after the next model upgrade".

## Ground rules
- Prime directive applies (CLAUDE.md): any change that could make the assessment less faithful is surfaced as
  **"Accuracy trade-off:"** and confirmed by the user before implementation.
- Every phase ends with: canary benchmark meeting its gate → `phases/NN-*.md` written → `STATE.md` updated → commit.
- Only the final phase runs the full six-vendor benchmark (cost control, see `TESTING.md`).
- One branch for the program (`accuracy-program`); the scoring engine is frozen until Phase 5.

## How gates work
- **Floor** = binding. A phase cannot close below a floor; floors protect against regressions (recall, residual ≤ inherent,
  verdict consistency, auditability). Never relaxed without an explicit user decision.
- **Target** = expected value, used to judge whether the change did what the recommendation predicted. Missing a target
  does not block closing the phase, but requires (a) a one-paragraph explanation in the phase file and (b) the user's sign-off
  recorded under "Open decisions" in `STATE.md`. Never a silent pass.

## Phases (canary = `orbitclear` throughout; full benchmark only in Phase 6)

### Phase 0 — Measurement (harness)
Scope: signal/noise classification as a judge step (categories from `testdata/_results/cases/fp_spec.md`) reported as
`signal_share`, `dup_per_golden`, `band_error`; flags `--skip-narratives`, `--judge none|match|full`; `JUDGE_MAX_TOKENS`
sized for 100+ findings; four new cases already in `benchmark/cases/`. No app change.
Floor: `bench run --cases orbitclear` reports the new metrics and reproduces the baseline recall 5/5.
Target: one canary run with judge=full ≤ 30k judge tokens.

### Phase 1 — Inputs & robustness (R7, R8) — model-independent
Scope: `assessment.as_of_date` (default today) in every prompt's "Analysis date" and in date logic; assessor standards
profile (small JSON: required attestations, refresh windows, retention target, residency, MFA policy) available to prompts;
gap-analysis failures persisted per control and resumable (no phase-level failure), per-control AI re-run endpoint;
long-questionnaire extraction per sheet/domain instead of the 80-finding cap; durable task registry; **dev-only LLM
response cache** keyed by `prompt_sha` + model (off by default, cannot be enabled in production config, bypassed by the
benchmark's real runs) so unchanged stages cost nothing while iterating — user-approved 2026-08-30.
Floor: canary run completes with no manual intervention; recall 5/5; no findings dated against the run date when
`as_of_date` is set.
Target: cost per canary run = new cost baseline (no increase over BASELINE beyond the judge step).

### Phase 2 — Whole-bundle assessment (R1) — appreciates with better models
Scope: gap analysis per scenario / control family with whole documents in context when the bundle fits the reasoner; each
distinct control code assessed once per assessment and reused across scenarios; FTS retrieval and the second retrieval pass
kept only as the fallback for oversized bundles; contradictions detected in the same whole-context call (both quotes required).
Floor: recall 5/5; per-control verdict consistency (same code → same verdict) 100 %; every citation still resolves to a chunk.
Target: gap-analysis tokens ≤ 40 % of Phase 1 baseline; wall time ≤ 10 min; ORB-G4 emitted as a contradiction citing the
EEA-only claim (DS-02/DS-03) and BC/DR §6.4; ORB-G5 emitted with ≥ 3 of its 5 signals linked; cross_doc_conflict duplicates 0.
**Decision point (recorded in STATE.md):** if the ORB-G4/G5 targets are met here, the claims/entity reconciliation part of
R2 is dropped from the program; only the attestation profile (Phase 4) remains.

### Phase 3 — Bundle-aware confirmation (R3) + thin dedupe (R4) — judgement, not rules
Scope: extraction outputs become candidates; one confirmation pass per document (or batch) with the bundle in context
answers "is this a deficiency of the vendor's control environment, given everything supplied?" → weakness / evidence-note /
dropped, every decision logged with a reason; questionnaire rows read as (question, response, comment); a *thin* merge pass
producing one row per deficiency with all quotes as evidence refs. No exclusion taxonomy (no "drop CUECs" rules) — the
prompt states the principle and the model judges.
Floor: recall 5/5; every dropped candidate logged (auditable suppression); no weakness without a resolvable quote.
Target: signal share ≥ 75 % (from 35 %); ≤ 1 duplicate per golden; spot check on verifypro (extraction + confirmation only):
"See comment" rows with affirmative comments no longer emitted as weaknesses.
Provisional-scaffold note: re-test with the merge pass disabled after the next model upgrade.

### Phase 4 — Attestation profile (thin R2) — durable product data
Scope: for SOC / ISO / pen-test documents only, one small typed record per document (type, period start/end, months
covered, first examination, opinion, carve-outs with named subservice orgs, CUEC count, test date, tester/accreditation,
scope) extracted with the fast profile and shown in the UI; deterministic checks against `as_of_date` and the standards
profile emit weaknesses / evidence notes with quotes. **No claims ledger, no entity reconciliation** unless the Phase 2
decision point says otherwise.
Floor: recall 5/5; every profile field carries a quote.
Target: QE-G6-class facts (short period, first examination, stale report) emitted deterministically on the canary
(6-month first Type 2) with correct dates; no staleness misreads from context tables.

### Phase 5 — Scoring & severity (R5, R6) — model-independent, LLM-free iteration
Scope: weaknesses change control *state* (coverage / effectiveness / operating-exception) instead of additive per-row
uplift; any remaining uplift bounded by distinct high/critical deficiencies; meta-issues become a reported confidence band;
residual ≤ inherent unless an independently evidenced failure maps to the scenario; aggregation revisited (weighted view
drives the band or ≥ 2 independent scenarios for Very High); severity assigned at confirmation time by consequence, with an
`evidence_strength` field (auditor-tested > vendor-admitted > inferred absence) stored on the weakness.
Iterate with `POST /recalculate` on stored assessments 6–11 (zero LLM cost) and the Phase 3/4 canary output.
Floor: no scenario with residual > inherent unless an audit/pen-test-evidenced failure maps to it; every band change on the
six stored assessments listed for the user; **user reviews band changes before merge (accuracy trade-off)**.
Target: band within one level of expected for ≥ 5/6 (cloudnimbus Moderate, quantedge Moderate, verifypro High,
globaltalent VeryHigh, meridian Moderate, orbitclear High); existing golden cases re-scored and reviewed.

### Phase 6 — Full validation
Scope: six vendors × 2 repetitions, judge=full; compare with `BASELINE.md`; closing report; list of provisional scaffolds to
re-test at the next model upgrade.
Floor: recall ≥ 35/36; no vendor's band further from expected than at baseline.
Target: signal share ≥ 80 %; ≤ 1 duplicate per golden; band within one level for ≥ 5/6; cost per vendor ≤ 50 % of baseline.
