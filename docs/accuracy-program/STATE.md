# STATE — read this first in every new context

**Program:** accuracy improvements, plan in `PLAN.md` (frozen), baseline in `BASELINE.md`, test protocol in `TESTING.md`.
**Branch:** not yet created (create `accuracy-program` from `main` @ d391c80 before the first code change).
**Current phase:** Phase 0 — not started. Plan revised to rev. 2 on 2026-08-30 (R1 first, R2 thinned).
**Last benchmark run:** run 5 (baseline, 2026-08-29). Canary reference: orbitclear, see BASELINE.md.
**Cost spent on program so far:** 0 tokens (baseline evaluation excluded).

## Phase checklist (PLAN rev. 2)
- [ ] Phase 0 — measurement (harness metrics, flags)
- [ ] Phase 1 — inputs & robustness (R7, R8)
- [ ] Phase 2 — whole-bundle assessment (R1) — decision point on the rest of R2
- [ ] Phase 3 — confirmation + thin dedupe (R3, R4)
- [ ] Phase 4 — attestation profile (thin R2)
- [ ] Phase 5 — scoring & severity (R5, R6) — user review of band changes required
- [ ] Phase 6 — full validation

## Open decisions for the user
- none yet

## Decisions taken
- 2026-08-30 · dev response cache is in scope for Phase 1 (user)

## Log (newest first; one line per stopping point: date · phase/step · run id or stage · key numbers · next action)
- 2026-08-30 · scaffold created; four new benchmark cases added (`bench list-cases` = 6) · no code changes · **next: create branch, start Phase 0 (harness metrics + flags)**
- 2026-08-30 · PLAN rev. 2: complexity/value + model-progress review; R1 promoted, R2 thinned to attestation profile; 03_improvement_recommendations.md carries a revision note
