# Phase 6 — Full validation

Started 2026-08-31 on `accuracy-program` @ `7ffba5a` (user go; concurrency 2 approved). Run 18: all six vendors ×
2 repetitions, judge=full, narratives on, `--cleanup none`, as_of 2026-05-01, empty standards profile (baseline parity).

## What is being validated
The whole program at once, against `BASELINE.md` (run 5):
- Floor: recall ≥ 35/36 across the 12 runs; no vendor's band further from expected than at baseline.
- Targets: signal share ≥ 80 %; ≤ 1 duplicate per golden; band within one level for ≥ 5/6; cost per vendor ≤ 50 % of
  baseline.
- Also recorded: repetition stability (bands and recall across the two reps), error/retry counts under concurrency 2
  (wall-clock timings are NOT comparable — two cases contend for API throughput; noted per TESTING).

## Deferred decision carried in
Rule B (a single auditor-tested high satisfies the uplift gate) is re-examined on this run's data: if underrating with
auditor-tested evidence mapped persists on reviewed weakness sets, adopt it (user decision from the Phase 5 review).

## Runs
Run 18 (9 ok / 3 error) + run 19 (verifypro r1 re-graded after the VP-G6 key amendment) + runs 20/21 (globaltalent r2
and verifypro r2 salvaged mid-pipeline — option B, user 2026-08-31). Failures fixed en route: harness extraction
timeout 600→1800 s (a per-stage wall clock at the time; replaced on 2026-09-04 by the liveness-based `IDLE_TIMEOUT_S` / `MAX_STAGE_S`); `scenario_controls` budget 4096→8192; merge pass splits on output truncation (globaltalent's
130-row merge overflowed 16k). globaltalent r1 abandoned (74k tokens spent; scenario truncation, fix applies forward).

| case | rep | recall | n | signal | dup/g | band | Δexp | exec |
|---|---|---|---|---|---|---|---|---|
| cloudnimbus | 1 | 2/3 | 9 | 78 % | 0.50 | Moderate | 0 | 88 |
| cloudnimbus | 2 | 2/3 | 5 | 60 % | 0.50 | Moderate | 0 | 88 |
| globaltalent | 2 | 7/9 | 86 | 94 % | 0.33 | VeryHigh | 0 | 88 |
| meridian | 1 | 4/7 | 15 | 67 % | 0.50 | Moderate | 0 | 94 |
| meridian | 2 | 6/7 | 26 | 96 % | 0.10 | High | +1 | 94 |
| orbitclear | 1 | 5/5 | 16 | 38 % | 1.00 | VeryHigh | +1 | 94 |
| orbitclear | 2 | 5/5 | 18 | 78 % | 0.40 | Moderate | −1 | 94 |
| quantedge | 1 | 5/6 | 18 | 100 % | 0.00 | Moderate | 0 | 78 |
| quantedge | 2 | 6/6 | 23 | 87 % | 0.17 | Moderate | 0 | 94 |
| verifypro | 1 | 6/7 | 51 | 20 % | 0.80 | VeryHigh | +1 | 94 |
| verifypro | 2 | 5/7 | 47 | 94 % | 0.60 | VeryHigh | +1 | 88 |

Union recall per vendor (either rep): cloudnimbus 2/3, globaltalent 7/9, meridian 6/7, orbitclear 5/5, quantedge 6/6,
verifypro 6/7 → **32/37 required goldens (86 %)**. Per-rep aggregate 53/65 (82 %).

The five union misses, with causes:
- **CN-G3** (TLS legacy cipher sunset): the baseline's own "partial" — an extraction blind spot, unchanged.
- **MER-W3** (no time-bound incident notification): golden added post-baseline; missed both reps.
- **GT-W7** (AWS carved out, no equivalent assurance): the attestation profile *captured* the carve-out (name + quote)
  but no check or review path elevates a carved-out primary hosting provider without assurance → follow-up check.
- **GT-W8** (pen-test highs without independent retest): extraction/confirmation gap.
- **VP-G8** (cold backup on provider-managed shared keys): never surfaced by extraction in any of three verifypro runs.

## Gate result vs BASELINE (run 5)
- **Floor "no vendor's band further from expected than at baseline" — MET** (every rep within one band of expected;
  baseline had four vendors at distance 2).
- **Floor "recall ≥ 35/36" — not evaluable as written and recorded as a qualified miss (user sign-off requested).**
  The key changed under the program: required goldens 36 → 41 (meridian/scaffold expansion) → 37 (VP-G6 removed as
  contradicting its own case standard, user 2026-08-31). Nearest equivalents: union 32/37 (86 %) vs the baseline's
  single-run 35/36 (97 %) on the smaller old key. Of the five misses, only CN-G3 existed at baseline (and was only
  "partial" then) — no demonstrated regression on baseline-era goldens; the other four are new goldens probing known
  extraction blind spots.
- **Target signal ≥ 80 % — mixed**: median 78 %, mean 74 % (7/11 reps ≥ 78 %; verifypro r1's 20 % is a weak-review
  outlier — its rep 2 scored 94 %). Baseline: 27.5 % canary / 49 % overall. Miss on the mean, recorded.
- **Target ≤ 1 duplicate per golden — MET** (mean 0.42, max 1.0; baseline 0.5–1.9).
- **Target band within one level for ≥ 5/6 — MET, 6/6** (baseline: 0/6 discrimination, all VeryHigh).
- **Target cost ≤ 50 % of baseline — near miss ≈ 54 %** (≈ 0.70 M tokens per pipeline vs ≈ 1.29 M baseline mean).
- Repetition stability: bands stable for cloudnimbus/quantedge/verifypro; meridian ±1; orbitclear ±1 both directions —
  driven by review-strictness and contradiction variance (signal 38 % vs 78 % across its reps), not by scoring.

## Rule B — closed
Re-simulated on the full validation data: it does NOT fix orbitclear r2 (its −1 is coverage/review variance, not the
uplift gate) and it pushes cloudnimbus r1 to High (expected Moderate). The Phase 5 gate stays as the user chose.

## Provisional scaffolds (re-test disabled after the next model upgrade)
Gap-analysis batch size 5 + split-on-truncation; reported-contradictions context at concurrency 2; the R4 merge pass
(+ its truncation split); confirmation batch splits; questionnaire window splitting; scenario_controls 8k budget.

## Follow-ups (post-program backlog)
1. Reasoner fallback when the fast-profile attestation extraction fails validation (flaky on ISO/pen-test docs;
   caused orbitclear r1's missing checks and rep instability).
2. Deterministic carve-out check: carved-out subservice org with no assurance evidenced anywhere → weakness (GT-W7).
3. Extraction calibration for the three persistent blind spots (legacy-cipher sunsets, backup-tier key custody,
   unretested pen-test findings).
4. Review-strictness variance across reps (verifypro 20 % vs 94 % signal) — candidate for a confirmation prompt
   calibration pass with examples, measured on `bench grade` re-runs.

## Program totals
≈ 13.8 M tokens: phases 0–4 ≈ 5.4 M, Phase 5 = 0, Phase 6 ≈ 8.4 M (run 18 6.03 M app + 0.55 M judge; salvage 1.72 M;
re-grades ≈ 0.1 M). Baseline projection for the same 12-run validation at baseline cost was ≈ 12 M for the runs alone.
