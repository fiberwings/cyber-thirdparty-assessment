# Phase 2 — Whole-bundle assessment (R1)

Started 2026-08-30 on `accuracy-program` after Phase 1 (`67e0a20`).

## Sizing (canary, assessment 13)
89 scenario×control targets → **35 distinct control codes** in 13 families; parsed bundle 70k chars ≈ 17k tokens
(largest vendor, meridian ≈ 23k). Phase 1 gap analysis: 89 + 18 second-pass calls, 457k in / 330k out tokens, 21.5 min.

## Design
- `WHOLE_BUNDLE_MAX_TOKENS = 100k` (approx len/4): below it every distinct code is assessed **once** against the whole
  bundle, in batches of ≤ 8 codes (families kept together) → ≈ 5 calls for the canary; above it the existing FTS
  per-control path (+ one second retrieval pass) runs unchanged as the fallback.
- One verdict per code is persisted to **every** scenario expecting that code (`_persist_control_output`, the persistence
  half of the old `assess_control`, now shared) → per-control verdict consistency is 100 % by construction.
- Contradictions are detected in the same whole-context call, one per disagreement under its most relevant control, both
  quotes required; persistence unchanged (dedupe by quote set; a second control appends its code).
- A code the model leaves out of a batch gets one *fill* call (`gap_analysis_bundle_fill`); still missing → recorded
  as failed on every scenario's control (R8 semantics, resumable with `only_failed`).
- Per-control API re-run (`assess-ai`) uses the bundle for its code and rewrites all scenarios of that code.
- Prompt `gap_analysis_bundle.md`: same verdict/citation/meta-flag rules as `gap_analysis.md` plus explicit whole-bundle
  contradiction hunting (residency vs replication/sub-processors/staff location; "no exceptions" vs audit exceptions;
  cadences differing between documents; sub-processor registers vs carve-outs; assurance claims vs a named provider).

## Accuracy trade-offs
- The scenario-specific wording of an expected control (LOG.SIEM appears in 9 scenarios with 9 rationales) is merged
  into one prompt block ("why it matters here: … | …"); the verdict is about the vendor's control, which is the same
  control in every scenario, so one verdict per code is the more faithful representation (the old path could rate the
  same control differently per scenario — 178 calls at baseline for 35 controls).
- Batching several controls into one output raises the chance of a truncated / partial answer: mitigated by 16k output
  budget, small batches, a fill call, and per-control failure persistence.

## Runs
| run | what | recall | n | signal | dup/golden | gap calls | gap tokens | gap wall | notes |
|---|---|---|---|---|---|---|---|---|---|
| 10 | `bench run … --judge full`, first whole-bundle build (batch 8, concurrency 4) → assessment **14** | 4/5 (G1, G3, **G4 ✓ as contradiction**, G5; **G2 missed**) | 36 | 22 % | 1.00 | 5 bundle | 115k/62k = **177k (22 % of Phase 1's 811k)** | **283 s** (Phase 1: 1291 s) | 1 of 5 batch calls truncated at the 16k output cap → 11 controls failed (resumable); verdict consistency 20/20 shared codes; 9 unresolved citations (all "…"-abbreviated list quotes); G5 reported under two controls in two batches (IAM.MFA + PRIV.SUBPROC, 2 quotes each) |

Fixes after run 10 (all in `gap_analysis.py` / `gap_analysis_bundle.md`): batch size 8 → 5 and a truncated batch is split in half and retried; quote
matching accepts "…"-abbreviated quotes when every fragment occurs in order (a real occurrence; prompt also forbids
abbreviating); batches run at concurrency 2 and each prompt lists the contradictions already reported by finished batches
("do not report again"); contradictions may cite up to five sources so a multi-signal disagreement is one row.
After re-run 1: quote binding made punctuation-insensitive (chunk text carries `**bold**` markers, table line breaks and
typographic dashes that a verbatim quote lacks; words must still occur contiguously and in order) in both the gap-analysis
and the extraction binder — the 7 citations left unresolved on assessment 14 all bind with it (read-only re-check).

| run | what | recall | gap calls | gap tokens | gap wall | contradictions | notes |
|---|---|---|---|---|---|---|---|
| 11 | gap analysis re-run on a14 (batch 5, split, concurrency 2, reported-context) + `bench grade --judge match` | **5/5** | 9 bundle | 200k/78k = **277k (34 %)** | **10 min 49 s** | G4 (3 sources: SIG DS-02/DS-03, SOC commitment, BC/DR §6.4), **G5 (3 sources: SOC subservice list, TPRM register, SIG TP-04)**, G2, DR-test discrepancy — **no duplicates** | 0 failed controls; 20/20 codes consistent; 7 unresolved citations (fixed afterwards, see above) |
| 12 | same at concurrency 3 | **5/5** | 9 bundle | 200k/85k = 285k (35 %) | **8 min 30 s** | G4 reported twice (two parallel batches, GOV.VENDOR + AUDIT.SOC2), G2, a BC/DR review-date discrepancy; G5 matched via a document-extraction row this time | duplicate shows the reported-context suppression needs batches to finish before the next start → **concurrency kept at 2** (accuracy over 2 min of wall) |

## Gate result
- **Floor recall 5/5 — met** (runs 11 and 12; the canary's first build, run 10, was 4/5 with a truncated batch).
- **Floor per-control verdict consistency 100 % — met** (20/20 shared codes; by construction).
- **Floor every citation resolves to a chunk — met**: the binder never binds a quote to a chunk that does not contain it;
  7 quotes the model wrote without the chunk's markdown/dash artefacts are now bound too (unit-tested + offline 7/7).
- **Target gap tokens ≤ 40 % of Phase 1 — met** (34 %); 9 calls instead of 109.
- **Target wall ≤ 10 min — missed by 49 s** (10:49 at concurrency 2; 8:30 at concurrency 3 but with a duplicate
  contradiction). Choice: keep concurrency 2. Explanation: the duplicate is an accuracy defect (two scored rows for one
  fact), the wall difference is not. **Needs user sign-off (Open decisions in STATE.md).**
- **Target ORB-G4 as a contradiction citing DS-02/DS-03 and BC/DR §6.4 — met** (run 11 and 12).
- **Target ORB-G5 with ≥ 3 of 5 signals linked — met** (run 11: 3 sources in one row).
- **Target cross_doc_conflict duplicates 0 — met at concurrency 2** (run 11), not at 3 (run 12).

## Decision point (PLAN Phase 2)
G4 and G5 targets met in whole-bundle mode → the claims-ledger / entity-reconciliation part of R2 is **dropped** from the
program; only the attestation profile (Phase 4) remains. Recorded in STATE.md.

## Provisional scaffolds (re-test with these disabled after the next model upgrade)
- Batch splitting on truncation and the 5-code batch size (a model with a larger reliable output could take all 35 codes
  in one call, which would also make cross-batch duplicates impossible).
- The "contradictions already reported" context + concurrency 2.

## Follow-ups pushed to later phases
- Phase 3 thin dedupe (R4) is the systematic answer to cross-batch contradiction duplicates.
- Extraction stage is now the wall-time bottleneck (documents extracted sequentially, 14–22 min on the canary).
