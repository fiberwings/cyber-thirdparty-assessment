# STATE — read this first in every new context

**Program:** accuracy improvements, plan in `PLAN.md` (frozen), baseline in `BASELINE.md`, test protocol in `TESTING.md`.
**Branch:** `accuracy-program`, created 2026-08-30 from `main` @ 0dedfe7 (scaffold commit; app code identical to d391c80).
**Program COMPLETE** (Phase 6 closed 2026-08-31; one qualified item open — recall-floor interpretation, see Open decisions). Closing report: `phases/06-full-validation.md`.
**Last benchmark run:** runs 18–21 (Phase 6 full validation + salvages + re-grade). Union recall 32/37; bands 6/6 within one of expected; signal median 78 %; dup 0.42; cost ≈ 54 % of baseline.
**Cost spent on program (final):** ≈ 13.8 M tokens (phases 0–4 ≈ 5.4 M; Phase 5 zero; Phase 6 ≈ 8.4 M).

## Phase checklist (PLAN rev. 2)
- [x] Phase 0 — measurement (harness metrics, flags, `bench grade`) — closed 2026-08-30
- [x] Phase 1 — inputs & robustness (R7, R8) — closed 2026-08-30 (recall 4/5 accepted, see Decisions)
- [x] Phase 2 — whole-bundle assessment (R1) — closed 2026-08-30; decision point: R2 claims/entity reconciliation dropped
- [x] Phase 3 — confirmation + thin dedupe (R3, R4) — closed 2026-08-31 (see Open decisions)
- [x] Phase 4 — attestation profile (thin R2) — closed 2026-08-31, all floors and targets met
- [x] Phase 5 — scoring & severity (R5, R6) — closed 2026-08-31 after user band review (option a)
- [x] Phase 6 — full validation — closed 2026-08-31 (recall-floor sign-off open)

## Open decisions for the user
- 2026-08-31 · **Phase 6 recall floor (qualified miss):** "≥ 35/36" is not evaluable on the expanded key (36→37 required after
  additions and the VP-G6 removal). Union recall 32/37 (86 %); no demonstrated regression on baseline-era goldens (sole
  baseline-era miss CN-G3 was already "partial" at baseline). Sign-off requested; misses + follow-ups in phases/06.
- 2026-08-30 · Delete partial assessment #12 on the backend (orphan of aborted run 6)? Harmless to keep.

## Decisions taken
- 2026-08-31 · Phase 6: salvage option B (globaltalent r2 + verifypro r2); VP-G6 golden removed — it contradicted the
  case's own stated refresh standard; the evidence (and the deterministic check) wins over the case narrative (user)
- 2026-08-31 · Phase 5 bands reviewed and approved; uplift gate kept as-is (option a: a17 −1 accepted); "rule B"
  (single auditor-tested high satisfies the uplift gate) to be re-examined on Phase 6's re-run data; aggregate
  rounding fixed to half-up (user)
- 2026-08-30 · dev response cache is in scope for Phase 1 (user)
- 2026-08-30 · do not re-run the pipeline when the app is unchanged; grade stored assessments with `bench grade` (user) —
  Phase 0 floor re-defined accordingly: grade of stored assessment 9 reports the new metrics and matches baseline recall 5/5
- 2026-08-30 · Phase 0 token target revised: judge=full ≈ 80k judge tokens per grade accepted (whole bundle to the classifier; gates only) (user)
- 2026-08-31 · Phase 3 signal 60 % vs ≥ 75 % target accepted (floors met; baseline 27.5 %; residual noise itemised) (user)
- 2026-08-30 · Phase 2 wall target miss (10:49 vs 10:00 at concurrency 2) accepted; concurrency 2 kept for duplicate-free contradictions (user)
- 2026-08-30 · Phase 2 decision point: G4 and G5 targets met by whole-bundle gap analysis → R2 claims ledger / entity
  reconciliation dropped; only the attestation profile (Phase 4) remains (per PLAN)
- 2026-08-30 · Phase 1 closed on recall 4/5 (run 9): ORB-G4 miss accepted as pre-existing ≈ 50 % extractor variance (2/4 stage
  re-runs), G4 remains the Phase 2 target (user, option a)
- 2026-08-30 · classifier prompt frozen at fc-2 (33/40 agreement with the manual classification on the canary)

## Log (newest first; one line per stopping point: date · phase/step · run id or stage · key numbers · next action)
- 2026-08-31 · Phase 6 closed, program complete · runs 18–21: bands 6/6 within one (floor met), dup 0.42 (met), signal median 78 % (mean 74 %, target miss), cost ≈ 54 % (near miss), union recall 32/37 (qualified floor miss, sign-off open) · rule B rejected on data · fixes en route: merge/scenario/extraction truncation robustness · follow-ups listed in phases/06 · **next: user sign-off on recall floor; backlog items 1–4**
- 2026-08-31 · Phase 5 closed after review (option a; rule B deferred to Phase 6; half-up rounding fixed) · committed · **next: Phase 6 full validation on user go**
- 2026-08-31 · Phase 5 implemented (0 LLM tokens) · engine: state-based downgrades, +1 bounded uplift, auditor-tested ceiling, meta→confidence, weighted-mean aggregation · rescore (DB copy): 6/6 within one band (was 0/6 discrimination), 0 floor violations · tests 85 pass, tsc clean · **next: user reviews band table → commit → Phase 6 (full validation)**
- 2026-08-31 · Phase 4 closed · runs 15/16 failed (schema tolerance; confirmation truncation) → fixes · run 17 (a17 salvaged): recall 5/5, n 14, SOC profile fully quoted, soc_short_first_examination deterministic with exact dates, 0 model staleness rows, app 527k/250k · tests 83/38 pass · **next: Phase 5 (R5/R6 scoring) — LLM-free iteration on stored assessments; user reviews band changes before merge**
- 2026-08-31 · Phase 3 closed · run 14 (canary a-stray-1): recall 5/5, 31→10 reported (8 dropped, 11 notes, 4 merged, all reasons logged), signal 60 %, dup 0.2, exec 94, app 440k/182k · spot check a15 (verifypro): 12 "See comment" misreads → ≤1, 97→47 reported · harness TIMEOUT_CORRELATE 600→1800 s; StageError keeps assessment id; questionnaire window splits on output truncation · tests 72/38 pass · **next: user sign-off on signal 60 % → commit already done → Phase 4 (attestation profile)**
- 2026-08-30 · Phase 2 closed · run 10 (a14, first build): recall 4/5, gap 177k/283 s, 1 batch truncated · fixes (batch 5, split, reported-context, punctuation-insensitive binding) · run 11: recall 5/5, gap 277k (34 %), 10:49, G4+G5 as multi-source contradictions, 0 dups · run 12 (conc. 3): 5/5, 8:30, 1 dup → concurrency 2 kept · tests 67 pass · **next: user sign-off on wall target → Phase 3 (R3 confirmation + R4 thin dedupe)**
- 2026-08-30 · Phase 1 closed (user accepted option a), committed · **next: Phase 2 (R1) — see phases/02-whole-bundle.md**
- 2026-08-30 · Phase 1 measured · run 9 (a13): recall 4/5 (G4), band +1, n 31, app 555k/436k, 39 min, phase completed with 1 resumable control · stage re-runs on a13: control resume ok; doc 59 ×4: leakage fixed 4/4, G4 emitted 2/4 · tests 58/38 pass, tsc clean · **next: user decides on the recall floor → commit → Phase 2 (R1)**
- 2026-08-30 · Phase 0 closed: user accepted ~80k judge-token target, committed · **next: Phase 1 (R7, R8) — see phases/01-inputs-robustness.md**
- 2026-08-30 · Phase 0 complete (uncommitted) · run 8 (`bench grade` a9, fc-2): recall 5/5, n 40, signal 27.5 %, dup/golden 0.50, band +1, exec 89, judge 82k tok · run 7 (fc-1): signal 62.5 %, judge 66k · run 6 aborted · tests 37 pass · **next: user reviews diff + signs off on the judge-token target miss → commit → Phase 1**
- 2026-08-30 · scaffold created; four new benchmark cases added (`bench list-cases` = 6) · no code changes · **next: create branch, start Phase 0 (harness metrics + flags)**
- 2026-08-30 · PLAN rev. 2: complexity/value + model-progress review; R1 promoted, R2 thinned to attestation profile; 03_improvement_recommendations.md carries a revision note
