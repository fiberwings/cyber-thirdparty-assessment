# Phase 4 — Attestation profile (thin R2)

Started 2026-08-31 on `accuracy-program` after Phase 3 (`8152573`). Scoring engine untouched. Per the Phase 2 decision
point, this is all that remains of R2 (no claims ledger, no entity reconciliation).

## Design
- `Document.attestation_profile` (JSON): one typed record per SOC / ISO / pen-test document —
  `schemas/attestation.py::AttestationProfileOut`: doc type, period start/end, report date, first examination, opinion,
  carve-outs (named subservice orgs), CUEC count, auditor, bridge letter, ISO issue/expiry + body, pen-test dates,
  tester, accreditation, scope. **Every populated field must carry a verbatim quote from the document itself** —
  a field without a quote is rejected by the schema (floor). The prompt forbids inferring dates from context tables or
  other documents' summaries — the input is only the attestation document.
- Extracted with the **fast profile** (`attestation_profile` purpose, ~1 call/doc) in the upload job after weakness
  extraction; re-runnable. Shown in the UI as a card on the document section.
- **Deterministic checks** (`app/attestation_checks.py`, pure/unit-tested) over (profile, `as_of_date`, standards
  profile), each finding carrying the profile quotes, run at the start of the cross-correlate step (idempotent —
  re-runs replace this origin's rows):
  - `soc_short_first_examination` (medium weakness): period < 9 months AND first Type 2 → "no earlier
    operating-effectiveness assurance exists" with the exact dates. `soc_short_period` (medium) without the first flag;
    `soc_first_examination` (evidence note) for a full-length first examination.
  - `attestation_stale`: period end older than `attestation_max_age_months` (standards; default 12) → medium weakness,
    high beyond 2×; a referenced bridge letter downgrades it to an evidence note.
  - `iso_certificate_expired` (high), `pentest_stale` (medium/high vs `pentest_max_age_months`, default 12).
  - `soc_opinion_qualified|adverse|disclaimer` (evidence note — the underlying exceptions are findings themselves).
  - Assessment-level `required_attestation_missing:<req>` (high) for each required attestation in the standards profile
    with no matching supplied document.
  Findings are Weakness rows (`origin="attestation_check"`, `kind_signal="attestation_check"`, status confirmed or
  evidence_note, review reason naming the check + analysis date, quotes bound to chunks with the Phase 2 matcher);
  weaknesses start unmatched so cross-correlation maps them onto scenario controls like any other finding.
- The soc/iso/pentest **extraction prompts no longer emit staleness findings for the document's own age** (the
  deterministic check owns that); they still shade other findings' severity by age. Policy / questionnaire staleness
  stays model-judged (their review cadence is a vendor statement, not an attestation fact).

## Accuracy trade-offs
- Staleness for assurance documents moves from model judgement to arithmetic over quoted dates: strictly more faithful
  (the baseline had three stale-SOC misreads from period-start columns of context tables) but depends on the fast
  profile quoting the right dates — mitigated by the quote-or-absent schema floor and by the checks staying silent when
  a field is absent (no fallback guessing; an absent field emits nothing rather than a wrong finding).
- Deterministic defaults (12-month attestation age, 9-month short period, 12-month pen-test age) apply only when the
  assessor standards do not state a value; they are constants in `attestation_checks.py`, not model behaviour.

## Runs
| run | what | recall | n | notes |
|---|---|---|---|---|
| 15 | canary attempt 1 — **failed at upload**: the fast model wrapped `doc_type` like the quoted fields and sent `bridge_letter: {value: false}` without a quote; the profile failure wrongly killed the extraction job | — | — | fixes: `doc_type` normaliser (dict/spelling variants → canonical, unknown → `other`), False needs no quote, profile extraction is best-effort (never fails the job; re-run endpoint `POST /documents/{id}/attestation-profile`) |
| 16 | canary attempt 2 — **failed at correlate**: one document's confirmation call overflowed 16k output (model rambling) | — | — | fixes: confirmation batch splits on truncation down to keep-and-flag (never fatal, never lost); terseness instruction |
| 17 | run-16 assessment (a17) salvaged from the correlate stage (the Phase 3 harness fix kept the assessment id) + `bench grade --judge match` | **5/5** all high (+ optional O2) | **14** confirmed (19 dropped, 11 notes, 1 merged; 0 unreviewed) | SOC profile extracted: type 2, period 2025-01-01→2025-06-30, report date, first examination, qualified opinion, 4 carve-outs incl. Nexus, 4 CUECs — **every field quoted**. Deterministic `soc_short_first_examination` (medium) emitted with the exact dates + `soc_opinion_qualified` note. **No model staleness rows for assurance docs.** In-run robustness: 2 gap-batch truncations + 1 confirmation truncation, all recovered by splits; 0 failed controls; verdicts consistent. App tokens 527k/250k; judge 6.5k. |

## Gate result
- **Floor recall 5/5 — met** (run 17).
- **Floor every profile field carries a quote — met** (schema-enforced: value without quote is rejected; canary profile
  clean; a True bool needs its quote, a False states an absence and needs none).
- **Target QE-G6-class facts emitted deterministically with correct dates — met** (6-month first Type 2 with
  2025-01-01 → 2025-06-30 verbatim-quoted; check code `soc_short_first_examination`).
- **Target no staleness misreads from context tables — met** (assurance-doc staleness is arithmetic over the document's
  own quoted dates; the extraction prompts no longer emit it; zero model staleness rows on the canary).

## Provisional scaffold note
None — the profile is durable product data (PLAN calls it out as structure the product needs regardless of model). The
doc-type normaliser is tolerant parsing, not judgement substitution.
