# Phase 1 — Inputs & robustness (R7, R8)

Started 2026-08-30 on `accuracy-program` after Phase 0 (`77e9d6c`). First phase that changes app code. Scoring engine
untouched (frozen until Phase 5).

## What was implemented

### R7 — analysis date and assessor standards as first-class inputs
- `Assessment.as_of_date` (ISO date, nullable = today) and `Assessment.standards_profile` (JSON) — `db.py` additive
  migration; `PATCH /api/assessments/{id}/settings` (`as_of_date` / `clear_as_of_date` / `standards_profile`);
  `AssessmentRead` exposes `as_of_date` (effective), `as_of_date_set`, `standards_profile`.
- `app/schemas/standards.py::StandardsProfile` — required attestations, attestation / pen-test max age, policy review
  cadence, retention target, allowed residency, MFA policy, vulnerability SLA, free-text requirements. Every field
  optional; `render()` is deterministic (one line per stated requirement) and an empty profile renders as
  *"(no assessor standards supplied — apply typical industry expectations…)"* so behaviour without a profile is unchanged.
- `app/ai/context.py` — `analysis_date()`, `analysis_datetime()`, `standards_block()`, `assessment_context_block()`.
  Used by **every** prompt builder: document extraction (temporal header), cross-correlation (analysis date + block),
  gap analysis (user block), scenario phases 1 and 2, executive summary, narrative (date only). Document extraction's
  "Document uploaded" line is now relative to the analysis date and is **withheld** when the upload post-dates a pinned
  analysis date (run artefact, not evidence); a one-line freshness rule anchors staleness on the analysis date only.
- Executive-summary fingerprint includes `as_of_date` + profile → changing either flags the summary stale.
- UI: `AssessmentSettings` panel on the scoping page (pin analysis date; profile form).
- Harness: `case.yaml` gains `as_of_date` / `standards_profile`; the runner PATCHes them before any stage. All six cases
  pinned to `as_of_date: 2026-05-01` (the date the test set was authored against — BASELINE freshness findings were
  artefacts of the run date).

### R8 — robustness that protects accuracy
- **Gap analysis no longer fails as a phase on per-control failures.** `run_full` returns `GapAnalysisResult`; a failed
  control is persisted on its `ControlAssessment` (`last_error`, `last_run_at`; a never-assessed control gets a row with
  coverage none / effectiveness unknown — exactly how `scoring.py` already scores a missing assessment, so the failure
  changes no score by itself). The phase completes with `warning` + `failed_targets` in `phase_state` / `PhaseInfo`.
  Only when *every* control fails does the run still raise (bad key / model down).
  Resume: `POST …/gap-analysis/run?only_failed=true`; per control: `POST /api/expected-controls/{id}/assess-ai`
  (respects user locks). UI: warning + "Re-run the N failed controls only" on the analysis page; "Re-run AI" +
  last-error banner in `ControlEditor`.
  *Deliberate deviation from R8's wording "surface them as meta-issues":* a MetaIssue would add likelihood uplift for a
  tool failure, i.e. make the assessment harsher for a reason unrelated to the vendor. Failures are surfaced on the
  control, the phase and the UI instead.
- **Long questionnaires:** the 80-finding phase-2 cap is gone (nothing is dropped any more; the two-phase path details
  every skeleton). Questionnaires that do not fit a single call — or whose single call truncates — fall back to
  *windowed direct extraction*: sheets/domains packed into ≤ 12k-token windows, ordinary questionnaire prompt per window,
  DB dedupe guard; purpose `document_weakness_extract_window`.
- **Durable task registry:** `task` table mirrors every handle (write-through on each update); `GET /api/tasks/{id}`
  answers from the table after a restart; startup `reconcile_interrupted_tasks()` marks pending/running rows as
  *"interrupted by server restart"* and fails the owning phase so the UI offers a re-run instead of hanging. Every submit
  is tagged with `kind` + `assessment_id`.
- **Dev-only LLM response cache** (user-approved 2026-08-30): `LLM_DEV_CACHE=1` + `APP_ENV != production`
  (`Settings.llm_dev_cache_active`). Key = sha256(model, full message list, temperature, max_tokens, response_format);
  table `llm_cache`; only complete (non-truncated) responses are stored; a hit is logged on `ModelCall` with
  `cached=True` and **zero tokens**. `/api/health` reports `llm_dev_cache`; `bench run` refuses such a backend unless
  `--allow-dev-cache` (plumbing only). Off by default — `.env` untouched.

### Tests
Backend: `tests/test_phase1_inputs_robustness.py` (10 new; partial failure persisted + resumable, all-failed still
raises, as_of/standards in the gap prompt, empty-profile wording, extraction header uses as_of not wall clock,
questionnaire windows without cap, cache off by default / refused in production / hit accounting, restart reconcile,
settings endpoint + per-control re-run over HTTP) → **58 passed**. Benchmark: settings applied before scenarios,
dev-cache refusal → **38 passed**. Frontend: `tsc --noEmit` clean.

## Accuracy trade-offs raised
1. **Dev response cache** — could let stale responses stand in for fresh ones. Mitigations above (off by default,
   impossible in production, benchmark refuses it, hits visibly logged). Approved by the user in STATE (2026-08-30).
2. **Partial gap-analysis completion** — an assessment can now be scored while N controls are unassessed (scored as
   coverage none, as before for a missing assessment). Previously the phase failed and blocked; now it completes with a
   visible warning and resumable controls. Net effect on faithfulness: neutral on the score, positive on completion
   (the baseline lost whole 25-min phases to 2 garbled calls). Documented here; no user decision needed under PLAN.
3. **Questionnaire windows** — a window sees only its sheets; cross-sheet context inside one questionnaire is not
   available to the extractor. Replaces silent dropping of findings beyond 80, which is strictly worse. Cross-document
   judgement is Phase 3's job.

## Runs
| run | what | recall | n | band | judge | app tokens in/out | wall | notes |
|---|---|---|---|---|---|---|---|---|
| 9 | `bench run --cases orbitclear --skip-narratives --judge match`, as_of 2026-05-01, empty profile → assessment **13** | **4/5** (G1, G2, G3, G5; **G4 missed**) | 31 | VeryHigh (+1) | match 4.8k/5.3k | **555k / 436k** (89 gap calls + 18 r2; 1 truncation failure) | 39 min (extraction 14 min sequential, gap 21.5 min) | phase completed with `warning` "88/89 controls assessed; 1 failed and can be re-run (IDP_SUBPROCESSOR_BYPASS/IAM.MFA)" — at baseline the same failure mode errored the whole phase (a9: "87/89 … 2 failed") |

Stage-only follow-ups on assessment 13 (TESTING §2), all at the fixed backend:
- `POST /api/expected-controls/885/assess-ai` — the failed control re-assessed (`full/weak`), `failed_targets` → `[]`, ~19k tokens.
- BC/DR policy (doc 59) re-extracted 4× (delete rows → `POST /api/documents/59/extract-weaknesses`, ~25k tokens each):
  - **run-date leakage**: run 9 had one finding reasoning from the upload date ("next-review date … has passed relative to the
    document upload date (2026-08-30)") although the header labelled the upload as after the analysis date. Fix: an
    upload that post-dates a pinned analysis date is now withheld from the prompt and a one-line freshness rule is added.
    4/4 re-extractions reason from "the analysis date (May 2026)"; no leakage.
  - **ORB-G4**: at baseline G4 was produced by *this extractor call* (a9 w225, `policy_gap`, "archive replicated to AWS
    us-east-2 … outside the EEA"), not by gap analysis. Same document, same chunk (988 present, cited as evidence): the
    single reasoner call emitted the US-replication finding in **2 of 4** samples (0, 0, 1, 1). Baseline's 1/1 was the
    lucky side of a ≈ 50 % coin — the miss is pre-existing extractor variance on a cross-document fact judged inside
    one document, the exact failure R1 (Phase 2, target "ORB-G4 emitted as a contradiction citing DS-02/DS-03 and BC/DR
    §6.4") exists to fix. Nothing in the Phase 1 header explains a 0/1 → 2/4 difference.
- Assessment 13 is therefore a *mutated* artefact (doc 59 rows replaced by the last sample, control 885 re-run);
  run 9's graded report is intact in the bench DB (`case_result.report_json`).

Cost: 555k/436k app tokens for the pipeline without narratives = the clean baseline (BASELINE canary reference ≈ 0.55 M /
0.4 M incl. narratives ≈ 30k) — **target met**; judge 10k (match).

## Gate result
- Floor "canary completes with no manual intervention" — **met** (and the R8 behaviour is what made it complete: one
  truncated control at baseline would have failed the phase).
- Floor "no findings dated against the run date when `as_of_date` is set" — **met after the header fix**
  (run 9 itself had 1 such finding; re-extraction 4/4 clean).
- Floor "recall 5/5" — **not met on run 9 (4/5)**. Root cause established above as pre-existing ≈ 50 % extractor variance
  on ORB-G4, not a Phase 1 effect. Floors are binding and may not be relaxed without an explicit user decision → **open
  decision in STATE.md**. Options: (a) accept run 9 + the 4-sample evidence and close Phase 1 (G4 is already Phase 2's
  target); (b) re-run the canary (~1 M tokens, ~40 min, ≈ 50 % chance of 5/5 — a coin flip, not information);
  (c) `bench grade` assessment 13 as it stands now (contains a G4 row from the last sample) — would read 5/5 but is
  cherry-picked; not recommended. Recommendation: **(a)**.
- Target "cost per canary run = new cost baseline" — **met** (no increase; narratives skipped, so the with-narratives
  figure ≈ +30k as before).

## Closed
2026-08-30 — user chose option (a): run 9 accepted with the 4-sample variance evidence; ORB-G4 stays the Phase 2 target.

## Follow-ups pushed to later phases
- Standards profile is *available* to prompts; deterministic checks against it (attestation age, SLA) are Phase 4.
- Phase 2 will re-visit `document_weakness_extract_window` once whole-bundle assessment lands.
