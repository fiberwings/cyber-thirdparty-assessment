# Phase 0 — Measurement (harness only)

Started 2026-08-30 on branch `accuracy-program` (from `main` @ 0dedfe7). **No app change** — every file touched is under
`benchmark/` or `docs/`.

## What was implemented

### Signal/noise classification judge step (`--judge full`)
- `bench/prompts.py`: `FINDING_CLASS_SYSTEM` / `finding_class_user` (version `fc-2`; fc-1 was the first calibration, see Runs), categories verbatim from
  `testdata/_results/cases/fp_spec.md`: `TP`, `TP_OPTIONAL`, `DUP_OF_TP`, `LEGIT_UNKEYED`, `BOILERPLATE`, `MISREAD`,
  `JUDGE_FN`, `JUDGE_FP_MATCH`; plus `missed_goldens[].fact_in_chunks` (ingestion vs reasoning problem).
- `bench/judge.py`: `Judge.classify_findings()` with `FindingClassOut` schema and structural checks (every reported id
  exactly once; golden required for TP/TP_OPTIONAL/DUP_OF_TP/JUDGE_FN/JUDGE_FP_MATCH and forbidden otherwise; at most one
  primary hit per golden — extra statements must be `DUP_OF_TP`). One validation-driven retry as for the other judge calls.
- The classifier sees: goldens (with `optional`), reported weaknesses enriched with `kind_signal`, `origin`,
  `source_chunk_id`, `evidence_refs`, the prior matching output, and the **evidence chunks** fetched via
  `GET /api/documents/{id}/chunks` (new `AppClient.get_chunks`). Whole bundle when it fits
  `JUDGE_CLASSIFY_CHUNK_BUDGET_CHARS` (120k chars ≈ 30k tokens; orbitclear ≈ 70k chars), otherwise only cited chunks
  (`chunk_scope` recorded as `full` / `cited` in `classification_json`).
- `bench/metrics.py`: `score_classification()` →
  `signal_share = (TP+TP_OPTIONAL+LEGIT_UNKEYED+JUDGE_FN)/n`, `dup_per_golden = DUP_OF_TP / distinct goldens hit`,
  `judge_fn`, `judge_fp_match`. Unit-tested against the manual orbitclear tabulation (35 % signal, 4 dups / 6 goldens).
  P/R/F1 deliberately **not** corrected by the classification — comparable with baseline run 5; `judge_fn > 0` flags an
  under-count.
- `band_error()` (deterministic, any judge mode): rank(app band) − rank(`golden.expected_band`), Low=1…VeryHigh=4.
  `expected_band` added to `cases.py` schema and to all six `case.yaml` (values from PLAN Phase 5 target).

### Flags
- `--judge none|match|full` (default `full`): `none` = no judge calls (band_error + n_weaknesses only, no API key
  needed); `match` = weakness matching only; `full` = matching + classification + exec rubric.
- `--skip-narratives`: pipeline stops after `/recalculate`; no `executive_summary` → exec rubric not graded.
  Both recorded in `run.config_json` (`judge_mode`, `skip_narratives`, `judge_classify_chunk_budget_chars`).

### Persistence / dashboard
- `case_result` gains `signal_share`, `dup_per_golden`, `judge_fn`, `classification_json`, `band_error`, `n_weaknesses`;
  `bench/db.py` now performs an additive column migration on open (existing `bench.sqlite` kept; run 5 rows untouched).
- Dashboard: run table shows n / Signal / Dup/G / ΔBand; case page shows the class per matched/extra finding, the full
  classification table and the missed-golden fact check. `judge_call.purpose` gains `finding_class`.
- Runner prints a one-line metric summary per case.

### JUDGE_MAX_TOKENS
8192 → **32768**. Baseline evidence: with `z-ai/glm-5.1` the match step already truncated at 8192 on quantedge
(~40 findings) and the exec rubric on meridian. Both match and classification emit one justified entry per finding
(~100 tokens each), so 100–120 findings (verifypro, globaltalent) need ≈ 12–15k output per call plus headroom.

### `bench grade` (added during the phase, user decision 2026-08-30)
The user questioned re-running the unchanged pipeline (~1 M app tokens) just to exercise the judge. Run 6 was aborted
(~90k in / 140k out app tokens spent; partial assessment #12 left on the backend, can be deleted) and replaced by
`bench grade --assessment ID --case CASE [--judge …]`: grades an assessment already on the backend — report + chunks via
HTTP, judge only, `config.mode = "grade"`, empty timings, `tokens_json` = the stored assessment's original cost.
`runner.py` refactored so `run_one` and `grade_stored` share `_grade_and_persist` / `_new_run` / `_finish_run`.
This is also the tool TESTING §2 needs from Phase 2 on (re-run one stage on the stored canary, then re-grade).
Per-call judge progress is now printed to stderr (purpose, attempt, latency, tokens, error).

### Tests
`benchmark/tests`: 37 passed (metrics: classification tabulation, JUDGE_FN/FP semantics, band_error; judge: classify
happy path, structural retry, rejection after two bad answers; integration: full mode with chunks, `--judge match
--skip-narratives`, `--judge none`, DB migration).

## Accuracy trade-offs raised
None affecting the assessment (no app change). One harness-measurement choice worth the user's eye: the classifier's
chunk context is budgeted (whole bundle ≤ 120k chars, else cited chunks only). On the canary the whole bundle is sent, so
MISREAD / LEGIT_UNKEYED verdicts are checked against the full evidence, as in the manual baseline classification.

## Runs (all on stored baseline assessment #9 = the BASELINE canary report, app 0dedfe7; judge z-ai/glm-5.1)
| run | what | recall | n | signal | dup/golden | judge_fn | band_error | exec | judge tokens in/out | classification agreement with manual (`fpclass_orbitclear.json`) |
|---|---|---|---|---|---|---|---|---|---|---|
| 6 | `bench run` canary — **aborted** by user after ~6 min (pipeline re-run unnecessary) | — | — | — | — | — | — | — | — | — |
| 7 | `bench grade`, classifier fc-1 | 5/5 | 40 | 62.5 % | 0.67 | 0 | +1 | 83 | 46.6k / 19.5k = 66k | 29/40; all 10 TP/TP_OPT/DUP verdicts identical; 11 one-directional lenient calls (BOILERPLATE/MISREAD → LEGIT_UNKEYED where the "missing" element sits in another document) |
| 8 | `bench grade`, classifier **fc-2** (whole-bundle check before LEGIT_UNKEYED; disclosed design choices = boilerplate) | 5/5 | 40 | **27.5 %** | 0.50 | 0 | +1 | 89 | 46.7k / 35.6k = 82k | **33/40**; residual 7 disagreements are bidirectional judgement calls (3 LEGIT→BOILER, 3 MISREAD→BOILER, 1 DUP→MISREAD) — none touch TP/recall |

Manual baseline for the same report: signal 35 %, 4 dups / 6 goldens = 0.67. fc-2 is slightly stricter than the manual
reviewer (27.5 %); read `signal_share` with roughly ±10 pp calibration uncertainty and compare **relative** to run 8, not
to the manual 35 %. `fc-2` is the frozen classifier prompt for the program.

Judge latency: fc-2 classification took 24 min (26k output tokens, mostly the model's own reasoning) — the harness kept
the connection open past the nominal 300 s httpx timeout because OpenRouter streams keep-alives. Acceptable at phase
gates; not for iteration (use `--judge match` or `none`).

## Gate result
- **Floor — met.** New metrics reported (`signal_share`, `dup_per_golden`, `band_error`, `judge_fn`, `n_weaknesses`);
  recall 5/5 reproduced on the baseline canary report (runs 7 and 8).
- **Target — missed: judge=full ≤ 30k judge tokens.** Actual 66k (fc-1) / 82k (fc-2). Cause: the classifier receives the
  whole evidence bundle (122 chunks ≈ 34k input tokens) so MISREAD / "covered elsewhere" verdicts are checked against the
  evidence — the exact capability that separated fc-1 from fc-2. The 30k figure was set before the classifier was
  designed. Options for the user: (a) accept ~80k per judge=full grade (used only at Phase 3 and 6 gates + 12 runs in
  Phase 6 ≈ 1 M judge tokens total); (b) `JUDGE_CLASSIFY_CHUNK_BUDGET_CHARS=0` → cited-chunks only (~45k) at the cost of
  fc-1-style leniency; (c) a cheaper/faster judge model for the classification call only. Recommendation: (a).
  **User decision 2026-08-30: option (a) accepted; target revised to ≈ 80k judge tokens per full grade.**

## Closed
2026-08-30 — committed on `accuracy-program` after user review.

## Follow-ups pushed to later phases
- Phase 3/6 use `signal_share` and `dup_per_golden` as targets; deterministic quote-substring checks (TESTING §4) are
  not yet scripted — add when Phase 3 iteration starts.
