# STATE — read this first in every new context

**Program:** accuracy improvements, plan in `PLAN.md` (frozen), baseline in `BASELINE.md`, test protocol in `TESTING.md`.
**Branch:** `accuracy-program`, created 2026-08-30 from `main` @ 0dedfe7 (scaffold commit; app code identical to d391c80).
**Current phase:** Phase 1 — inputs & robustness (R7, R8) — started 2026-08-30. Plan rev. 2.
**Last benchmark run:** run 8 (2026-08-30, `bench grade` of stored assessment 9, classifier fc-2). Canary reference: orbitclear, see BASELINE.md; **Phase 0 reference numbers = run 8** (signal 27.5 %, dup/golden 0.50, band_error +1, exec 89).
**Cost spent on program so far:** ≈ 380k tokens — app: ≈ 230k (aborted run 6); judge: 66k (run 7) + 82k (run 8).

## Phase checklist (PLAN rev. 2)
- [x] Phase 0 — measurement (harness metrics, flags, `bench grade`) — closed 2026-08-30
- [ ] Phase 1 — inputs & robustness (R7, R8)
- [ ] Phase 2 — whole-bundle assessment (R1) — decision point on the rest of R2
- [ ] Phase 3 — confirmation + thin dedupe (R3, R4)
- [ ] Phase 4 — attestation profile (thin R2)
- [ ] Phase 5 — scoring & severity (R5, R6) — user review of band changes required
- [ ] Phase 6 — full validation

## Open decisions for the user
- 2026-08-30 · Delete partial assessment #12 on the backend (orphan of aborted run 6)? Harmless to keep.

## Decisions taken
- 2026-08-30 · dev response cache is in scope for Phase 1 (user)
- 2026-08-30 · do not re-run the pipeline when the app is unchanged; grade stored assessments with `bench grade` (user) —
  Phase 0 floor re-defined accordingly: grade of stored assessment 9 reports the new metrics and matches baseline recall 5/5
- 2026-08-30 · Phase 0 token target revised: judge=full ≈ 80k judge tokens per grade accepted (whole bundle to the classifier; gates only) (user)
- 2026-08-30 · classifier prompt frozen at fc-2 (33/40 agreement with the manual classification on the canary)

## Log (newest first; one line per stopping point: date · phase/step · run id or stage · key numbers · next action)
- 2026-08-30 · Phase 0 closed: user accepted ~80k judge-token target, committed · **next: Phase 1 (R7, R8) — see phases/01-inputs-robustness.md**
- 2026-08-30 · Phase 0 complete (uncommitted) · run 8 (`bench grade` a9, fc-2): recall 5/5, n 40, signal 27.5 %, dup/golden 0.50, band +1, exec 89, judge 82k tok · run 7 (fc-1): signal 62.5 %, judge 66k · run 6 aborted · tests 37 pass · **next: user reviews diff + signs off on the judge-token target miss → commit → Phase 1**
- 2026-08-30 · scaffold created; four new benchmark cases added (`bench list-cases` = 6) · no code changes · **next: create branch, start Phase 0 (harness metrics + flags)**
- 2026-08-30 · PLAN rev. 2: complexity/value + model-progress review; R1 promoted, R2 thinned to attestation profile; 03_improvement_recommendations.md carries a revision note
