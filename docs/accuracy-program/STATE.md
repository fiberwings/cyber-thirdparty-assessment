# STATE — read this first in every new context

**Program:** accuracy improvements, plan in `PLAN.md` (frozen), baseline in `BASELINE.md`, test protocol in `TESTING.md`.
**Branch:** `accuracy-program`, created 2026-08-30 from `main` @ 0dedfe7 (scaffold commit; app code identical to d391c80).
**Current phase:** Phase 2 — whole-bundle assessment (R1) — closed 2026-08-30 (one target sign-off pending); Phase 3 next. Plan rev. 2.
**Last benchmark run:** run 12 (2026-08-30, `bench grade` of assessment 14 after gap re-run #2; recall 5/5). Phase 2 canary = run 10 (a14) + gap re-runs graded as runs 11/12. Phase 0 reference (signal/dup) = run 8 on assessment 9 (signal 27.5 %, dup/golden 0.50).
**Cost spent on program so far:** ≈ 3.0 M tokens — app: ≈ 230k (run 6) + 991k (run 9) + ≈ 120k (a13 re-runs) + ≈ 700k (run 10 pipeline) + 277k + 285k (a14 gap re-runs); judge: 66k + 82k + 10k + 62k (run 10 full) + 13k + 10k.

## Phase checklist (PLAN rev. 2)
- [x] Phase 0 — measurement (harness metrics, flags, `bench grade`) — closed 2026-08-30
- [x] Phase 1 — inputs & robustness (R7, R8) — closed 2026-08-30 (recall 4/5 accepted, see Decisions)
- [x] Phase 2 — whole-bundle assessment (R1) — closed 2026-08-30; decision point: R2 claims/entity reconciliation dropped
- [ ] Phase 3 — confirmation + thin dedupe (R3, R4)
- [ ] Phase 4 — attestation profile (thin R2)
- [ ] Phase 5 — scoring & severity (R5, R6) — user review of band changes required
- [ ] Phase 6 — full validation

## Open decisions for the user
- 2026-08-30 · **Phase 2 wall-time target missed by 49 s** (10:49 vs ≤ 10 min at bundle concurrency 2; 8:30 at concurrency 3 but
  with a duplicated contradiction). Kept concurrency 2 (accuracy over speed). Sign-off requested; see phases/02 §Gate.
- 2026-08-30 · Delete partial assessment #12 on the backend (orphan of aborted run 6)? Harmless to keep.

## Decisions taken
- 2026-08-30 · dev response cache is in scope for Phase 1 (user)
- 2026-08-30 · do not re-run the pipeline when the app is unchanged; grade stored assessments with `bench grade` (user) —
  Phase 0 floor re-defined accordingly: grade of stored assessment 9 reports the new metrics and matches baseline recall 5/5
- 2026-08-30 · Phase 0 token target revised: judge=full ≈ 80k judge tokens per grade accepted (whole bundle to the classifier; gates only) (user)
- 2026-08-30 · Phase 2 decision point: G4 and G5 targets met by whole-bundle gap analysis → R2 claims ledger / entity
  reconciliation dropped; only the attestation profile (Phase 4) remains (per PLAN)
- 2026-08-30 · Phase 1 closed on recall 4/5 (run 9): ORB-G4 miss accepted as pre-existing ≈ 50 % extractor variance (2/4 stage
  re-runs), G4 remains the Phase 2 target (user, option a)
- 2026-08-30 · classifier prompt frozen at fc-2 (33/40 agreement with the manual classification on the canary)

## Log (newest first; one line per stopping point: date · phase/step · run id or stage · key numbers · next action)
- 2026-08-30 · Phase 2 closed · run 10 (a14, first build): recall 4/5, gap 177k/283 s, 1 batch truncated · fixes (batch 5, split, reported-context, punctuation-insensitive binding) · run 11: recall 5/5, gap 277k (34 %), 10:49, G4+G5 as multi-source contradictions, 0 dups · run 12 (conc. 3): 5/5, 8:30, 1 dup → concurrency 2 kept · tests 67 pass · **next: user sign-off on wall target → Phase 3 (R3 confirmation + R4 thin dedupe)**
- 2026-08-30 · Phase 1 closed (user accepted option a), committed · **next: Phase 2 (R1) — see phases/02-whole-bundle.md**
- 2026-08-30 · Phase 1 measured · run 9 (a13): recall 4/5 (G4), band +1, n 31, app 555k/436k, 39 min, phase completed with 1 resumable control · stage re-runs on a13: control resume ok; doc 59 ×4: leakage fixed 4/4, G4 emitted 2/4 · tests 58/38 pass, tsc clean · **next: user decides on the recall floor → commit → Phase 2 (R1)**
- 2026-08-30 · Phase 0 closed: user accepted ~80k judge-token target, committed · **next: Phase 1 (R7, R8) — see phases/01-inputs-robustness.md**
- 2026-08-30 · Phase 0 complete (uncommitted) · run 8 (`bench grade` a9, fc-2): recall 5/5, n 40, signal 27.5 %, dup/golden 0.50, band +1, exec 89, judge 82k tok · run 7 (fc-1): signal 62.5 %, judge 66k · run 6 aborted · tests 37 pass · **next: user reviews diff + signs off on the judge-token target miss → commit → Phase 1**
- 2026-08-30 · scaffold created; four new benchmark cases added (`bench list-cases` = 6) · no code changes · **next: create branch, start Phase 0 (harness metrics + flags)**
- 2026-08-30 · PLAN rev. 2: complexity/value + model-progress review; R1 promoted, R2 thinned to attestation profile; 03_improvement_recommendations.md carries a revision note
