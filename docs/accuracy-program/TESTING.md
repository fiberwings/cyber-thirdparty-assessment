# Test protocol — minimise API cost

## Principles
1. **One canary vendor during development: `orbitclear`.** Five gaps spanning Easy → Very Hard, six documents (~17k tokens),
   exercises every failure family found in the evaluation (claim vs SOC exception, policy contradiction, residency, entity
   reconciliation, SOC literacy, staleness). Nothing else runs end-to-end until Phase 6.
2. **Test the stage you changed, not the pipeline.** The app persists every stage; drive the changed stage on the *existing*
   canary assessment instead of re-running from scratch:
   - extraction / confirmation / attestation profile (Phases 3–4): `POST /api/documents/{id}/extract-weaknesses` on the stored orbitclear documents; compare weakness rows before/after (SQL) — no scenarios, no gap analysis, no narratives.
   - gap analysis (Phase 2): `POST /api/assessments/9/gap-analysis/run` only.
   - scoring / severity (Phase 5): `POST /recalculate` on ids 6–11 — **zero LLM calls**, all six vendors, instant.
   - dedupe (Phase 3): unit tests on the stored weakness rows (export them once as a fixture under `backend/tests/fixtures/`).
3. **Skip what is not being measured.** Narratives + executive summary (~30k tokens/run) only at phase gates that need the
   rubric (Phase 3 and 6). Harness flags to add in Phase 0: `--skip-narratives`, `--judge none|match|full`.
4. **Judge sparingly.** During iteration use the free deterministic check (`scripts`: golden quote substrings present in
   weakness quotes/evidence refs, weakness count, dedupe-key collisions, bands from `/recalculate`). Judge=match (one call,
   ~5k tokens) at each phase gate; judge=full only at Phase 3 and Phase 6.
5. **Cheap models where the task is extraction, not judgement.** The ledger extraction (R2) is designed for the fast profile;
   never move a reasoner stage to the fast profile without an "Accuracy trade-off:" check.
6. **Record every run.** Bench run id (or the manual stage run + assessment id) with tokens and the metrics in `STATE.md`.
   The harness's `model_call` collection gives tokens for free.
7. **Dev response cache (optional, Phase 1):** a dev-only cache keyed by the existing `prompt_sha` + model in `router.py`
   so re-running an unchanged stage costs nothing (e.g. iterating on dedupe while extraction prompts are stable). Must be
   off by default and impossible to enable in production config. Worth it if Phase 2–3 iteration exceeds ~5 canary runs.
8. **Unit tests over live calls.** Reconciliation, dedupe, date logic, scoring and severity rules are deterministic — cover
   them with pytest using stored fixtures; the fake-LLM e2e test (`backend/tests/test_e2e.py`) guards the API surface.

## Expected spend
Baseline canary full run ≈ 1 M tokens. Stage-only iteration ≈ 50–150k tokens (extraction) or 0 (scoring). Phase gates ≈ one
canary run each (Phase 3 onward much cheaper once R1 lands in Phase 2). Full validation (Phase 6): 6 vendors × 2 reps ≈ 12 runs at the
post-R1 cost, expected < 3 M tokens total vs ≈ 12 M for the same at baseline cost.

## Commands
```
cd backend && .venv/bin/uvicorn app.main:app --port 8010          # dedicated backend for testing
cd benchmark && BENCH_BACKEND_URL=http://localhost:8010 .venv/bin/bench run --cases orbitclear --cleanup none [--skip-narratives --judge match]
cd backend && .venv/bin/pytest -q                                   # deterministic tests
```
