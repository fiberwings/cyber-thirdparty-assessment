# Phase 3 — Bundle-aware confirmation (R3) + thin dedupe (R4)

Started 2026-08-30 on `accuracy-program` after Phase 2 (`4a87f55`). Scoring engine untouched.

## Design (judgement, not rules)
- **Extraction produces candidates** (`Weakness.status = "candidate"`). Candidates are not reported, mapped or scored:
  `Assessment.weaknesses` is now the *reported* set (`status == "confirmed"`, view-only relationship); every row lives in
  `Assessment.all_weaknesses` (owns the cascade). Legacy rows, gap-analysis contradictions and user rows are confirmed.
- **Confirmation** (`app/ai/agents/confirmation.py`, prompt `weakness_confirmation.md`): one reasoner call per source
  document with the WHOLE bundle + the analysis date + the assessor standards in context, answering one question per
  candidate — "is this a deficiency of the vendor's control environment, given everything supplied, that a diligent
  assessor would report?" → `confirmed` / `evidence_note` / `dropped`, with confidence and a one-sentence reason citing
  the decisive source. The prompt states principles (read questionnaire rows as question+response+comment; a gap another
  document closes is not a gap; keep admissions and independent test results; when unsure confirm with low confidence)
  — no exclusion taxonomy. A candidate the model does not decide on is **confirmed and flagged** (`review.unreviewed`),
  never lost.
- **Thin merge** (`weakness_merge.md`): one call over the confirmed document-origin rows → groups of "same underlying
  deficiency"; the primary keeps every member's quote as an evidence ref, the union of control codes and the highest
  severity; members become `status = "merged"` with `merged_into`. Conservative by instruction ("when in doubt, do not
  merge").
- Both run at the start of the cross-correlate step (`cross_correlation.run`), so only reviewed rows are correlated.
  **Consequence:** the per-upload auto cross-correlation is removed (it would review candidates against a partial
  bundle); the evidence phase completes when the user runs step 2 on the analysis page (the benchmark already did).
- Every decision is persisted on the row (`review` JSON) and visible: `GET …/weaknesses?include=all`, evidence page rows
  carry a status badge + reason; document headers show "N not reported".
- Questionnaire prompt: rows are (question, response, comment); a pointer answer whose comment confirms the control is
  not a weakness.

## Accuracy trade-offs
- This is the first phase that removes findings from the report. Protections: nothing is deleted (status + reason);
  low-confidence keeps rather than drops; unreviewed candidates are kept and flagged; the review sees the whole bundle
  and the client's standards; contradictions from gap analysis are not subject to the review (they are already
  whole-bundle judgements); user-edited rows are never touched.
- Merge changes the primary's description to the consolidated text (member evidence preserved as refs). Members remain
  readable with their own text.
- Cost: +1 reasoner call per document with the bundle in context (~25k tokens each) + 1 merge call.

## Runs
| run | what | recall | reported n | signal | dup/golden | exec | app tokens | notes |
|---|---|---|---|---|---|---|---|---|
| 13 | `bench run … --judge full` (narratives on) | — | — | — | — | — | — | **harness** stage timeout: cross-correlate now includes the 6 confirmation calls (sequential, whole bundle) and exceeded the harness's 600 s stage limit while the verifypro spot check ran in parallel; the backend task completed anyway. Harness `TIMEOUT_CORRELATE` raised to 1800 s; a `StageError` now keeps the assessment id on the case row. |
| 14 | the run-13 assessment finished through the API (gap analysis 2.5 min, narratives) + `bench grade --judge full` | **5/5** (all matched high) | **10** (from 31 candidates: 8 dropped, 11 evidence notes, 4 merged into 3 primaries) | **60 %** | **0.20** | **94** | 440k / 182k (confirmation 164k/40k; gap 174k/17k) | classification: 5 TP, 1 DUP (the 6-month SOC period, a second element of G5), 1 MISREAD (staff-location "contradiction"), 2 BOILERPLATE, 1 LEGIT_UNKEYED |

Review audit on the canary (every decision carries a reason; none unreviewed): dropped = "covered by another supplied
document" (BC/DR content in the InfoSec policy, RBAC via SOC AC controls, training tracking via SIG PG-05, IRP
classification), one restatement of the qualified opinion; evidence notes = the 4 CUECs, the 3 standard carve-outs
(AWS/Okta/Datadog with assurance in the register), a disclosed archive-RTO design choice, the 6-month SOC period, and the
optional HYOK item (ORB-O3, now an evidence note); merges = Nexus (2 rows → 1), policy review dates (3 → 1), stale bundle
(2 → 1). Recall on the required goldens unaffected.

## Spot check (verifypro, TESTING §2) — extraction + confirmation only, fresh assessment 15
97 candidates → 47 confirmed, 19 dropped, 22 evidence notes, 9 merged (~215k app tokens). The questionnaire windowed
extraction initially failed on this SIG (output larger than the 16k budget for a single window) → windows now split by
section on output truncation, nothing capped. **Target met:** baseline reported 12 "See comment"-type misreads on this
vendor; now 3 such candidates → 1 confirmed (backup-restore cadence, with a substantive reason), 2 evidence notes. The
10 SIG drops are all cross-document refutations with named sources (e.g. SIG A.1 "No" vs SOC CC3.1 + ISP §3; "no
sub-processor list" vs the context document's list; "no capacity management" vs SOC A1.2 tested-no-exceptions).

## Gate result
- **Floor recall 5/5 — met** (run 14; all five required goldens matched at high confidence).
- **Floor every dropped candidate logged — met** (0 non-confirmed rows without a reason, canary and spot check).
- **Floor no weakness without a resolvable quote — met** (0 reported rows without a chunk-bound quote).
- **Target ≤ 1 duplicate per golden — met** (0.20).
- **Target spot check ("See comment" + affirmative comment not emitted) — met** (12 → at most 1, justified).
- **Target signal share ≥ 75 % — missed at 60 %** (n = 10: 5 TP + 1 LEGIT_UNKEYED signal vs 1 DUP + 1 MISREAD +
  2 BOILERPLATE noise). Explanation: with only 10 reported rows each residual noise row costs 10 points; the remaining
  four are one restatement of G5's second element, one staff-location misread, and two policy-gap rows the classifier
  judged covered elsewhere. Phase 4's attestation profile addresses the first; the rest are candidates for prompt
  calibration in Phase 6. Measured against baseline: signal 27.5 % → 60 %, reported rows 40 → 10, duplicates/golden
  0.5–0.67 → 0.2. **Needs user sign-off — Open decisions in STATE.md.**

## Provisional scaffold note
Re-test with the merge pass disabled after the next model upgrade (PLAN); the confirmation pass itself is a whole-bundle
judgement call and stays.

Incident: the backend used for run 13 had been restarted from `frontend/` (relative `DB_PATH`), so its assessment lives
in a stray `frontend/data/tprm.sqlite` (gitignored; copy kept in the session scratchpad). Numbers above are from that
DB; the graded report is in the bench DB (run 14). Backend restarted from `backend/` before the spot check.
