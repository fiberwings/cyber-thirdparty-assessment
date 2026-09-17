# Architecture

This document describes how the Cyber TPRM Assessment app is built, how an assessment moves
through its phases, how the AI is used so that the result is faithful to the evidence at the
lowest cost that does not compromise that, and how large evidence files are handled so that
accuracy does not depend on the context window of whichever model is configured.

Everything here is derived from the code on the `accuracy-program` branch. Where a number is
quoted (a token threshold, a batch size, a timeout) it is the shipped default and the source
file is named so it can be checked.

Related documents:

- [`README.md`](../README.md) — what the product does, quick start.
- [`docs/BUILD.md`](BUILD.md) — dependencies, versions, building from source.
- [`benchmark/README.md`](../benchmark/README.md) — the accuracy benchmark harness.
- [`docs/accuracy-program/`](accuracy-program/) — the measured accuracy programme that
  produced the current pipeline (plan, baseline, per-phase reports, state log).
- [`CLAUDE.md`](../CLAUDE.md) — the accuracy-first rules every change must respect.

---

## 1. System overview

```
┌─────────────────────────┐        /api/* rewrite         ┌──────────────────────────────┐
│  frontend/  (Next.js 15)│ ───────────────────────────▶  │  backend/  (FastAPI)          │
│  App Router, React 19   │  polls tasks (800 ms) and     │  app/api        HTTP routes   │
│  TanStack Query, Tailwind│ assessment phases (1.5 s)    │  app/workflow   step guards   │
└─────────────────────────┘                               │  app/tasks      job registry  │
                                                          │  app/ai/router  LLM client    │
                                                          │  app/ai/agents  pipeline      │
                                                          │  app/parsing    PDF/XLSX/DOCX │
                                                          │  app/scoring    deterministic │
                                                          └───────┬──────────────┬───────┘
                                                                  │              │ HTTPS (streamed SSE)
                                                       ┌──────────▼──────┐  ┌────▼──────────────┐
                                                       │ SQLite (WAL)    │  │ OpenRouter or      │
                                                       │ data/tprm.sqlite│  │ Azure AI Foundry   │
                                                       │ + FTS5 index    │  │ /chat/completions  │
                                                       │ storage/ files  │  │ per model ref      │
                                                       └─────────────────┘  └────────────────────┘

┌─────────────────────────┐
│  benchmark/  (separate) │  HTTP-only driver + LLM judge + dashboard (:8100); never imports
│  bench CLI, dashboard   │  backend code. See benchmark/README.md.
└─────────────────────────┘
```

| Component | Technology | Notes |
|---|---|---|
| Frontend | Next.js 15 (App Router), React 19, TypeScript, Tailwind 3, TanStack Query 5 | All API calls go to relative `/api/...` paths; `next.config.mjs` rewrites them to `BACKEND_URL` (resolved at **build** time). |
| Backend | FastAPI, SQLAlchemy 2, Pydantic 2, httpx | Single process, single user. Background jobs run on the event loop in a task registry; no external queue. |
| Database | SQLite in WAL mode with `busy_timeout=5000` | One file (`DB_PATH`). Additive schema migrations run at startup (`app/db.py`, `_ADDITIVE_COLUMNS`). An FTS5 virtual table `chunk_fts` is kept in sync with `chunk` by triggers. |
| File storage | Content-addressed directory (`STORAGE_DIR/<sha256>/<filename>`) | Downloads are HMAC-signed with `STORAGE_SECRET`. |
| LLM access | OpenRouter (`OPENROUTER_API_KEY`) and/or Azure AI Foundry (`AZURE_OPENAI_*`, `AZURE_INFERENCE_*`) | Two named profiles, `fast` and `reasoner`, each mapped to a model ref whose scheme picks the provider (`azure:` / `foundry:` / bare = OpenRouter); every call is streamed through one engine. |
| Benchmark | Separate Python package under `benchmark/` | Drives the app over HTTP with prepared vendor cases and grades the output. |

### Backend module map

| Path | Responsibility |
|---|---|
| `app/main.py` | App factory, CORS, `/api/health`, root logging (`LOG_LEVEL`), startup (schema migration, interrupted-task reconciliation, model capability warning, watchdog). |
| `app/config.py` | Every setting, with validation of the budget and liveness policies. |
| `app/workflow.py` | Canonical step order, prerequisite guards (409), mutual exclusion, stale-on-write invalidation. |
| `app/tasks.py`, `app/activity.py` | Task registry (in-memory + durable `task` table), structured progress, liveness watchdog, cancellation, SSE stream. |
| `app/ai/router.py` | The LLM client: one streaming engine, liveness deadlines, JSON-schema validation, retry ladder, provider content-filter detection, per-attempt forensics, dev cache, cost telemetry (`model_call` table). |
| `app/ai/providers/*` | Provider dialects behind the engine: `openrouter.py`, `azure.py` (Azure OpenAI deployments and the Foundry Models endpoint), `registry.py` (model-ref parsing, `AZURE_DEPLOYMENT_META`, credential and startup checks). A dialect owns only the URL, auth, body parameters and how its stream labels the upstream and its content filter. |
| `app/ai/agents/*` | One module per AI stage (see §3). |
| `app/ai/prompts/*.md` | System prompts, loaded by name. |
| `app/ai/catalog/controls.json` | The standard control catalogue: 47 controls in 15 families (Identity, Data, Cryptography, Vulnerability Management, Assurance, …). |
| `app/ai/retrieval.py` | FTS5 BM25 retrieval (fallback path of gap analysis). |
| `app/ai/context.py` | Analysis date and assessor standards block rendered into every prompt. |
| `app/parsing/*` | PDF (PyMuPDF), XLSX (openpyxl), DOCX (python-docx), plain text → page/section-aware chunks. |
| `app/attestation_checks.py` | Pure, LLM-free checks over attestation profiles (freshness, short SOC period, first examination, expired ISO certificate, qualified opinion, required attestations). |
| `app/scoring/engine.py`, `tables.py` | Deterministic 4×4 scoring and aggregation. No LLM. |
| `app/models/entities.py` | SQLAlchemy tables (see §8). |
| `app/schemas/ai.py`, `api.py`, `attestation.py`, `standards.py` | Pydantic schemas for model output, the HTTP API, the attestation profile and the assessor standards profile. |
| `app/api/*` | Routers: assessments, scoping, documents, document_weaknesses, scenarios, gap_analysis, weaknesses, scoring, report, models, tasks. |

---

## 2. The assessment workflow

An assessment is a strict chain of six steps. Each step consumes the persisted output of the
one before it. The order, the prerequisites and the invalidation rules live in one place,
`backend/app/workflow.py`; the frontend only renders what the backend reports.

```
scoping → scenarios → evidence → correlation → analysis → narratives → (report)
```

| # | Step (backend key) | UI page (left nav) | Trigger | Job kind | Model profile | Produces |
|---|---|---|---|---|---|---|
| 1 | `scoping` | 1. Scoping | `POST /api/assessments/{id}/description`, then `POST .../scoping/turn` (repeat) or `.../scoping/force-continue` | `scoping_turn` | fast | Service description, sufficiency scores on 7 dimensions, a final summary. |
| 2 | `scenarios` | 2. Inherent risk | `POST .../scenarios/generate` | `scenarios_generation` | reasoner | Inherent-risk scenarios (impact × likelihood 1–4) each with expected controls. |
| 3 | `evidence` | 3. Evidence | `POST .../documents` (upload); extraction starts automatically | `document_extraction` (one per document) | reasoner (+ fast for the attestation profile) | Parsed chunks, candidate weaknesses per document, typed attestation profile for SOC / ISO / pen-test documents. |
| 4 | `correlation` | 4. Analysis, step 1 | `POST .../cross-correlate` (alias `POST .../weaknesses/synthesize`, which the UI calls) | `cross_correlation` | reasoner | Deterministic attestation findings; candidates confirmed / noted / dropped against the whole bundle; duplicates merged; confirmed weaknesses mapped to scenario controls; emergent scenarios. |
| 5 | `analysis` | 4. Analysis, step 2 | `POST .../gap-analysis/run[?only_failed=true]` | `gap_analysis` | reasoner | Per-control coverage / effectiveness verdicts with citations, contradictions, meta-issues. |
| 6 | `narratives` | 4. Analysis, step 3 | `POST .../narratives/run` (also `POST .../executive-summary/run`) | `narratives`, `executive_summary` | fast (narratives), reasoner (summary) | Score-explanation prose per scenario; executive summary with verdict, key risks, limitations, recommended actions. |
| – | score / report | 5. Residual score, 6. Report | `GET .../report` (recalculates on every read) | – | none | Residual bands, aggregate band, confidence, printable report. |

Notes on the table:

- **Scoring is not a step.** `POST .../recalculate` and `GET .../report` re-run the deterministic
  engine on every call, in well under a millisecond. Editing a control verdict re-renders the
  band immediately.
- The evidence step has no single job: it is *done* when every uploaded document has a
  `weakness_extracted_at` stamp, *running* while any document extraction is live, and *error* if
  any extraction failed (the evidence page offers a per-document retry). The stamp is written only
  when every call of the extraction succeeded — a two-phase extraction with one failed detail call
  keeps the findings it did detail (the re-run dedupes on the quote key) but leaves the document
  in *error*, so a partially extracted document never passes downstream as complete.
- Uploading is allowed while another document is still extracting (same step); every other
  mutation is refused while any job runs for the assessment.

### 2.1 Enforcement

Three rules are enforced by the backend and mirrored in the UI as `ready` / `blocked_by` per phase:

1. **Prerequisites.** A step may start only when every upstream step is done *and not stale*.
   Otherwise the request is refused with HTTP 409 and a structured reason (`missing_description`,
   `prerequisite_pending`, `prerequisite_running`, `prerequisite_error`, `prerequisite_stale`,
   `no_documents`, `extraction_incomplete`, `extraction_failed`, `resume_requires_full_run`).
2. **Mutual exclusion.** One AI job per assessment. A second request for the *same* step
   re-attaches to the run in flight; a request for a different step is refused (`run_in_flight`).
   Input edits are refused while a job runs because the job may be writing the rows being edited.
3. **Stale-on-write invalidation.** When an input changes, every downstream step that had
   completed is stamped `stale` in `Assessment.phase_state` with the reason (kept visible in the
   UI) and blocks progression until re-run. Stamps are written by the mutator, never inferred.

What stamps what (from the API routers):

| Change | Stale from |
|---|---|
| Scoping answer / description edit (a no-op if the text is identical) | scenarios |
| Scenarios regenerated | correlation |
| Document uploaded, deleted, re-extracted; attestation profile re-extracted | correlation |
| Cross-correlation re-run | analysis |
| Gap analysis re-run | narratives |
| Scenario or control *text* edited (feeds the gap-analysis prompt) | analysis |
| New expected control added | analysis (resumable with `only_failed=true`) |
| Inherent ratings, weights, deletions, verdict edits | narratives |
| Assessor standards profile changed | scenarios |
| Analysis date changed | correlation (per-document extraction is deliberately *not* re-run; the stale reason says so) |

### 2.2 Background jobs and liveness

Long-running steps are submitted to the task registry (`app/tasks.py`) and answered with a task
id. The registry keeps an in-memory handle for the live side (progress events, cancellation) and
writes every status change through to the `task` table so `GET /api/tasks/{id}` still answers
after a restart. On startup, tasks that were pending or running when the process died are marked
`error` ("interrupted by server restart") and the phase that owned them is failed so the UI offers
a re-run instead of hanging.

Progress is *observed*, never estimated: each agent declares its stages and unit counts
(`activity.stage(...)`, `activity.advance(...)`), and the LLM router pings the ambient task on
every streamed token or keepalive. The frontend's AI activity indicator polls `GET /api/tasks/{id}`
every 800 ms and shows the stage stepper, units done, calls in flight, streamed tokens and idle
time; it draws a determinate bar only when a unit count is known. An SSE stream
(`GET /api/tasks/{id}/events`) exists as well but the shipped UI polls.

A watchdog cancels a task with no activity for `TASK_IDLE_TIMEOUT_S` (600 s) or older than
`TASK_MAX_RUNTIME_S` (14 400 s); the user can cancel with `POST /api/tasks/{id}/cancel`.
Cancellation closes the model stream (stopping generation and billing on providers that support
it) and fails the owned state exactly as a restart would. See §6 for the LLM-side deadlines.

---

## 3. Stage by stage

### 3.1 Scoping (fast profile)

`app/ai/agents/scoping.py`, prompt `scoping.md`. The analyst writes a description; each turn the
model scores seven dimensions 0–5 (data types, hosting, network access, identity flow, regulatory
scope, geography, criticality), asks **one** open question that closes the largest gap, and
declares sufficiency when every dimension is ≥ 3. The analyst can force-continue at any time.
The final `summary_so_far` is what scenario generation consumes.

### 3.2 Scenario generation (reasoner)

`app/ai/agents/scenarios.py`, prompts `scenarios_phase1.md` / `scenarios_phase2.md`. Two phases:

1. One call returns scenario *skeletons* (code, name, description, inherent impact and likelihood).
2. One focused call **per scenario** returns its expected controls, choosing codes from the
   control catalogue where applicable, each with a weight and a rationale. Phase-2 calls run 4-way
   in parallel, each in its own DB session, so a failure in one scenario never loses another's
   controls.

The two-phase design keeps each output small (truncation risk low) and lets the model reason
about one scenario's controls at a time. Regeneration deletes the previous description-sourced
scenarios first, so a re-run is clean; emergent scenarios (created later by correlation) are kept.

### 3.3 Evidence: parsing, extraction, attestation profile

**Parsing** (`app/parsing/`). Every upload is stored, then split into chunks that carry enough
metadata for a citation to render as "file — page 7 — Section 3.1.2":

| Format | Parser | Chunking |
|---|---|---|
| PDF | PyMuPDF | Per page; headings detected by font size (≥ 1.18 × the page's median body size, < 200 chars) maintain a running `section_path`; blocks are packed up to 1 500 characters. |
| XLSX | openpyxl | Per sheet; the header row is the first row with ≥ 2 non-empty cells; each data row is rendered as `header: value \| …`; up to 6 rows or 1 500 characters per chunk; `section_path` = `Sheet 'X' (rows a-b)`. |
| DOCX | python-docx | Word heading hierarchy becomes `section_path` (`H1 > H2`); tables are flattened to pipe rows; 1 500-character packing. |
| TXT / MD / CSV, unknown | built-in | Paragraph packing to 1 500 characters. |

Chunks are indexed in the FTS5 table for the retrieval fallback (§5.3). **Nothing is dropped at
parse time**: the whole document is chunked and stored.

**Per-document weakness extraction** (`app/ai/agents/document_weaknesses.py`, reasoner). Starts
automatically after upload. The prompt is chosen by the document *kind* the analyst selected
(`questionnaire`, `soc`, `iso`, `pentest`, `policy`, `other`); each prompt says what counts as a
finding for that kind (a SOC Section IV exception, a pen-test finding, a "no" or hedged
questionnaire answer, a missing baseline element in a policy, a missing DPA clause) and demands a
verbatim quote of ≤ 35 words plus the `section_path` and page of the finding. Output rows are
stored as **candidates** (`Weakness.status = "candidate"`); they are not scored until
cross-correlation has reviewed them against the whole bundle. How large documents are handled here
is the subject of §5.

**Attestation profile** (`app/ai/agents/attestation.py`, fast profile, best effort). For SOC,
ISO and pen-test documents one small typed record is extracted after the weaknesses: document
type, period start / end, first examination, opinion, carve-outs, CUEC count, bridge letter,
certificate expiry, test dates, tester, scope. Every field carries its own quote. A failed profile
never fails the upload; the error is stored on the document, shown on the evidence page with a
re-run action, and reported as an evidence note during correlation so the reader knows the
document was supplied but not machine-checked.

### 3.4 Cross-correlation

`app/ai/agents/cross_correlation.py` with `confirmation.py` and `attestation.py`. One job, five
stages in this order:

1. **Attestation checks** (deterministic, `app/attestation_checks.py`). Pure arithmetic over the
   stored profiles, the analysis date and the assessor standards: SOC period shorter than 9 months
   (weakness; a first-examination variant), attestation older than the allowed age (12 months by
   default; evidence note instead of weakness if a bridge letter is referenced; high severity past
   twice the allowance), pen test older than allowed, ISO certificate expired, qualified / adverse
   / disclaimer opinion (evidence note), and required attestations not supplied. Each finding
   carries the profile's quotes. Re-runs replace the previous rows.
2. **Confirmation** (reasoner, prompt `weakness_confirmation.md`). One call per source document
   with the **whole evidence bundle** in context. For every candidate the model answers one
   question — is this a deficiency of the vendor's control environment given everything supplied?
   — and returns `confirmed`, `evidence_note` or `dropped` with a one-sentence reason, a
   confidence, a re-assessed severity by consequence, and an `evidence_strength`
   (`auditor_tested` > `vendor_admitted` > `inferred_absence`). The decision and reason are
   persisted on the row. A candidate the model does not decide on is **confirmed and flagged for
   human review**, never silently lost.
3. **Merge** (reasoner, prompt `weakness_merge.md`). One thin pass over the confirmed
   document-origin rows groups rows that describe the same deficiency: one primary row keeps
   every member's quote as an evidence reference, members are flagged `merged` (kept for audit,
   never deleted).
4. **Correlation** (reasoner, prompt `cross_correlation.md`). Confirmed, still-unmapped
   weaknesses are bucketed by `kind_signal` and sent in clusters of ≤ 8 (4-way parallel). For each
   cluster the model maps weaknesses onto existing scenarios' expected-control codes (only codes
   that already drive scoring are accepted), or proposes an **emergent scenario** with its own
   expected controls when a cluster fits no existing scenario. A weakness mapped only to catalogue
   codes that no scenario expects is kept unmatched and recorded as an `unscored_finding`
   meta-issue so it is visible rather than lost.
5. **Accuracy floor** (deterministic). Any high or critical weakness still unmatched spawns a
   forced emergent scenario per `kind_signal` (`WEAK_<KIND>`, inherent likelihood 3, impact = the
   worst severity) with a single remediation control, so high-severity findings always reach the
   residual band even if the model declined to place them.

### 3.5 Gap analysis

`app/ai/agents/gap_analysis.py`, prompts `gap_analysis_bundle.md` (whole-bundle mode) and
`gap_analysis.md` (retrieval mode). For every expected control the model decides
`coverage` (none / partial / full — does it exist on paper) and `effectiveness`
(weak / adequate / strong / unknown — is it operating), with citations **required** for partial or
full coverage, `meta_flags` for gaps in the evidence (`vague_answer`, `insufficient_info`,
`missing_doc`) and `contradictions` between sources (each side quoted).

Two modes, chosen by bundle size (§5.3):

- **Whole-bundle mode** (default when the parsed bundle is ≤ 100 000 approximate tokens). Every
  *distinct control code* is assessed **once per assessment** against the complete bundle, in
  batches of up to 5 related codes (a family such as `IAM.*` is kept together when it fits). The
  verdict is written to every scenario that expects the code, so verdicts are consistent by
  construction and a code shared by four scenarios costs one assessment instead of four. Batches
  run 2-way parallel and share a list of contradictions already reported, which is fed to later
  batches so the model suppresses cross-batch duplicates in context. A code the model leaves out
  gets one fill call; a batch whose output truncates even at the enlarged budget is split in half
  and retried, down to single codes.
- **Per-control retrieval mode** (fallback for oversized bundles). For each scenario × control,
  the top 8 chunks by BM25 (`FTS_TOPK`) for the control code, name, description and scenario name
  are retrieved; the model assesses on those; if it found nothing and proposed better search terms,
  exactly one second retrieval and re-assessment happens. 4-way parallel.

Persistence rules (both modes):

- A citation is bound to a chunk **only if the quoted words actually occur in that chunk**
  (punctuation- and markdown-insensitive, in order, contiguous; abbreviated quotes with "…" must
  match every fragment in order). The model's `chunk_id` is trusted only when that holds; otherwise
  the cited document is searched; if the quote is nowhere, it is stored as an *unresolved citation*
  on the control and surfaced to the executive summary as an evidence-quality limitation. Evidence
  is never attached to text it does not appear in.
- A contradiction becomes exactly one **scored weakness** (`kind_signal = cross_doc_conflict`)
  with both quotes as evidence references; its identity is the set of disagreeing quotes, so the
  same disagreement seen under two controls collapses into one row that lists both codes. A
  contradiction that merely restates a weakness already mapped to the control is suppressed so the
  same failure is not scored twice.
- Meta flags become `MetaIssue` rows per control (replaced on re-run, never stacked).
- Failures are **per control**: the error is stored on `ControlAssessment.last_error`, the phase
  still completes with a warning listing the failed targets, and they can be re-run individually
  (`POST /api/expected-controls/{id}/assess-ai`) or together (`only_failed=true`). Only a run where
  *every* call failed is a run-level failure. The phase's `failed_targets` list is the contract
  for anything that scores the result: the benchmark reads it after the stage and records the
  case as invalid (`error_stage = gap_analysis`, `gap_failed_controls`) rather than grading a
  report whose F1 would measure the outage.
- A control the analyst has edited (`is_locked_by_user`) is never overwritten by the model.

### 3.6 Scoring (deterministic)

`app/scoring/engine.py`. No model is involved; the prose is written *after* scoring. Per scenario:

1. Weaknesses change the **state** of the controls they map to rather than adding points: a
   high / critical weakness forces effectiveness to `weak`; a medium caps it at `adequate`; low
   changes nothing. Each downgrade is recorded (`state_downgrades`) for audit.
2. Each control's (coverage, effectiveness) pair maps to an effectiveness score in [0, 1] (for
   example full/strong = 1.0, full/adequate = 0.8, partial/unknown = 0.25, none/* = 0); the
   weight-averaged result is the **coverage index**; `likelihood_reduction = round(index × 3)`.
3. **Uplift** is 0 or +1: +1 only when ≥ 1 critical or ≥ 2 distinct high / critical deficiencies
   map to the scenario.
4. `residual_likelihood = inherent − reduction + uplift`, clamped to 1–4 and **capped at inherent**
   unless at least one of those deficiencies is `auditor_tested`, in which case it may exceed
   inherent by one. Impact never changes.
5. The band comes from the 4×4 matrix (`tables.py`; the frontend copy is asserted identical by a
   test). Meta-issues produce a **confidence** label (high / medium / low), reported and never
   scored.

Aggregate band = the impact-weighted mean of scenario band ranks, rounded half-up; Very High
additionally requires the weighted mean to round there or ≥ 2 independent scenarios at Very High.
Aggregate confidence is the worst scenario confidence. The top-2 mean is reported for reference
but does not drive the band.

### 3.7 Narratives and executive summary

- **Narratives** (`narrative.py`, fast profile, plain text): one short paragraph per scenario
  explaining the residual score from the control verdicts, their citations and the scenario's
  meta-issues.
- **Executive summary** (`executive_summary.py`, reasoner, prompt `executive_summary.md`): the
  only place the pipeline asks a model to *prioritise*. Given the aggregate, every scenario, every
  weakness (both sides of each contradiction), the meta-issues, the unresolved citations and the
  document list, it writes the verdict, ranked key risks (each tied to scenario codes and weakness
  ids), limitations and recommended actions. Scenario codes or weakness ids the model invented are
  stripped and each removal is appended to `limitations` — surfaced, never masked. The stored
  summary carries a fingerprint of the scored state; the report API flags it `stale` after any
  edit or recalculation instead of silently re-spending a reasoner call.

### 3.8 Report

`GET /api/assessments/{id}/report` recalculates and returns everything in one payload: the
assessment and its settings, the description, documents, scored scenarios with control verdicts
and evidence, weaknesses, meta-issues, the aggregate and the executive summary with its staleness
flag. The score page renders the residual-risk matrix (inherent → residual movement per
scenario), the register and the per-scenario explanations; the report page is the printable
version.

---

## 4. How the AI is used: accuracy first, cost second

The product's prime directive (see `CLAUDE.md`) is that the assessment must be faithful to the
evidence. The design choices below follow from it; the cost savings are real but secondary.

### 4.1 Two model profiles, assigned per task

| Profile | Default model | Used for | Why |
|---|---|---|---|
| `reasoner` | `anthropic/claude-opus-4.7` (`MODEL_REASONER`) | Scenario generation, weakness extraction, confirmation, merge, cross-correlation, gap analysis, executive summary | Every judgement that decides what is reported or what the score is. |
| `fast` | `anthropic/claude-haiku-4.5` (`MODEL_FAST`) | Scoping Q&A, attestation-profile extraction, per-scenario narrative prose | Narrow tasks whose output is either validated structurally (the profile) or explanatory only (prose after scoring). |

Both are **model refs** and can be changed in `.env`; the alternatives listed in
`MODEL_*_ALTERNATIVES` populate the per-assessment **Settings → Model routing** page, where each
of six stages (`scoping`, `scenarios`, `gap_analysis`, `weaknesses`, `narrative`,
`executive_summary`) can be overridden (`PATCH /api/assessments/{id}/model-overrides`). Moving a
reasoner stage to the fast profile is an accuracy trade-off and is treated as such.

A model ref's scheme decides the provider per call, so OpenRouter and Azure can be mixed freely
(`app/ai/providers/registry.py`):

| Ref | Provider | Sent as `model` | Capability lookup |
|---|---|---|---|
| `anthropic/claude-opus-4.7` or `openrouter:…` | OpenRouter (`/api/v1/chat/completions`, Bearer key, `provider.ignore`) | the id | the id |
| `azure:<deployment>` | Azure OpenAI deployment (`{AZURE_OPENAI_ENDPOINT}/openai/v1/chat/completions`, `api-key`, `max_completion_tokens`) | the deployment | the canonical id from `AZURE_DEPLOYMENT_META` |
| `foundry:<deployment>` | Foundry Models endpoint (`{AZURE_INFERENCE_ENDPOINT}/models/chat/completions`, `api-key`, `extra-parameters: pass-through`, `max_tokens`) | the deployment | idem |

An Azure deployment name says nothing about the model behind it, so `AZURE_DEPLOYMENT_META`
(`<deployment>=<canonical id>[;temp=fixed],…`) declares it; a deployment used as a profile
default without an entry is a startup ERROR because the ladder ceiling and the guard below depend
on it. `temp=fixed` omits the `temperature` parameter for deployments that only accept their
default (o-series / GPT-5 reasoning reject the profile's 0.2 with a 400 that names the flag): an
explicit sampling change, warned at startup and recorded per attempt as `temperature_sent: null`
— the router never strips a parameter on its own. Azure reports token counts but no cost, so
those calls are recorded with `cost_usd = 0`, `cost_source = ""` and no under-reporting warning.

A **capability guard** (`router.py`, `MODEL_CAPS`) checks configured models against a local table
of context windows and output caps: a reasoner model must offer ≥ 200 000 context tokens and an
output cap ≥ `LLM_MIN_MODEL_OUTPUT_CAP` (128 000), a fast model ≥ 100 000 context and an output
cap ≥ `LLM_BUDGET_MEDIUM` (its starting budget); unknown ids only warn, so novel models remain
usable, while a ref that cannot be routed at all (unknown scheme, provider credentials not set)
is rejected. The truncation ladder (§4.2) also clamps at the catalogued output cap, so a model
that stops inside the ladder fails loudly at its own limit rather than with a provider error.

### 4.2 Structured output that is validated, never patched

Every non-prose call goes through `call_structured` in `app/ai/router.py`:

1. The request asks for `response_format: json_object`; the response is parsed and validated
   against a Pydantic schema specific to the call.
2. A **schema miss** (coherent JSON with a wrong or missing field) is retried once with the
   model's bad output and the validator error appended, so the model corrects itself.
3. **Garbled** output (not JSON-object-shaped, or no recognisable schema field) is retried once
   with fresh context — feeding garbage back reproduces it. Garbled twice fails loudly.
4. A response with `finish_reason == "length"` is **never parsed or persisted**. The router retries
   with a doubled output budget up to `LLM_TRUNCATION_RETRIES` (2) times, clamped at
   `LLM_TRUNCATION_CAP` (128 000) and at the model's catalogued output cap, then raises
   `truncated=True` so the *caller* can split its input (§5.4). Silent budget inflation and partial
   parsing are both prohibited. Budgets bound the model's *hidden reasoning* as well as its answer
   (providers count thinking tokens against `max_tokens`; the reasoners measured in 2026-09 spent
   70–85 % of their output thinking), which is why the tiers are sized well above the JSON they
   produce.
5. A response the **provider's content filter** cut is treated the same way, whatever
   `finish_reason` says: OpenRouter normalises such stops to `stop`, so the router checks the
   provider's `native_finish_reason` (`sensitive`, `content_filter`, …), never parses or caches the body
   (a cut at a JSON-valid point would pass as a complete, shorter answer), retries once with fresh
   context, then raises `filtered=True` naming the provider. `OPENROUTER_PROVIDER_IGNORE` routes
   around hosts known to do this (StreamLake on glm-5.3-flash cut a data-residency finding naming
   Hong Kong SAR, 2026-09). Azure's filter is stricter on security text and configured per
   deployment: a cut completion arrives as `finish_reason: content_filter` with the category in
   `content_filter_results` (surfaced as native reason `content_filter:<category>/<severity>`),
   and a rejected *prompt* as HTTP 400 `content_filter` — both are `filtered=True`, the remedy
   being the deployment's content-filter policy in Foundry or another deployment.
6. Every logical call, successful or not, is recorded in `model_call` with purpose, model, tokens
   (input, output, cached, reasoning) and metered cost summed across all of its attempts, latency,
   time to first token, how the last attempt finished (`finish_reason`, the provider's
   `native_finish_reason`, `provider`, `generation_id`) and a per-attempt summary
   (`attempts_json`: layer, requested budget, outcome, dialect, temperature sent, token split). On failure it also keeps the
   first 8 000 and last 2 000 characters of output, every retry and the final failure are logged at
   WARNING, and with `LLM_FAILURE_DUMP_DIR` set the complete output of each attempt is written to
   disk — enough to tell a torn `stop` body from a `length` cut without re-running the call.

### 4.3 Citation discipline

- Extraction, gap analysis and contradiction prompts all require a **verbatim quote of ≤ 35
  words** plus the location (`document_id`, page or `section_path`).
- Gap-analysis citations are stored only when the quote is found in a chunk (§3.5); otherwise
  they are kept as unresolved and reported. The same matcher binds attestation-check quotes.
- The gap-analysis schema rejects `partial` / `full` coverage without at least one citation, so a
  claim of coverage cannot be persisted uncited.
- Weakness rows keep the source chunk, the quote, and (after merge or contradiction detection) a
  list of evidence references; the UI renders every one as a clickable citation to the chunk text.

### 4.4 Say "unknown" when unknown

- Gap-analysis prompts instruct: base the verdict only on the supplied evidence; never infer
  coverage from reputation or industry norms; use `insufficient_info` rather than fabricating
  absence; `effectiveness` cannot be `strong` for a control the vendor states inconsistently.
- Meta flags describe gaps in the **evidence**, never disagreements between sources (those are
  vendor findings). They become a confidence label, not a score change (§3.6).
- The confirmation prompt says: when genuinely unsure, confirm with `confidence: "low"` — a
  doubtful deficiency is for the human reviewer to drop, not for the model to hide. Every dropped
  candidate keeps its reason on the row (auditable suppression).

### 4.5 Two inputs the model must never guess

`app/ai/context.py` renders both into every prompt as a header:

- **Analysis date** (`Assessment.as_of_date`, default today). Every freshness judgement and every
  deterministic date check uses it, never the wall clock. Uploads that post-date a pinned analysis
  date are withheld from the freshness header entirely so no judgement can anchor on them.
- **Assessor standards profile** (`schemas/standards.py`): required attestations, attestation
  and pen-test maximum age, policy review cadence, retention target, allowed residency, MFA policy,
  vulnerability SLA, other requirements. An empty profile renders as "no assessor standards
  supplied — apply typical industry expectations" so the model knows which regime it is in.

### 4.6 Deterministic wherever a rule is better than a judgement

Scoring, the 4×4 matrix, attestation date arithmetic, the high/critical accuracy floor,
duplicate suppression at the database (a unique `(assessment_id, dedupe_key)` index), citation
binding, and reference stripping in the executive summary are all plain code. The model is asked
for judgement where judgement is the task — what counts as a deficiency given the whole bundle,
which control a finding maps to, whether two sources disagree.

The opposite rule also holds (`docs/accuracy-program/PLAN.md`, "design stance"): prefer a
whole-context judgement call that must cite quotes over hand-built rule lists, entity ledgers or
heuristic dedupe, unless the benchmark shows the model cannot do it. Scaffolds that compensate for
model limits (batch splitting, the merge pass, reported-contradiction context, questionnaire
windows) are marked provisional and re-tested when models improve.

### 4.7 Human control

Every AI verdict is editable and edits are protected: a user-edited control verdict is locked
against re-runs, scenarios and controls can be added, edited and deleted, and any edit
recalculates the score instantly. (Weakness rows carry a `user_edited` flag that every re-run
honours, but the current API exposes no weakness-editing route.) Manual
edits stamp the affected downstream steps stale so the narrative cannot silently describe a state
that no longer exists.

### 4.8 Where the cost goes, and how it is kept down without losing accuracy

| Mechanism | Effect |
|---|---|
| Once-per-code gap analysis | A control code expected by *n* scenarios is assessed once, not *n* times; the Phase 2 canary cut gap-analysis tokens to ≈ 34 % of the per-control baseline while raising recall. |
| Batches of related codes over one bundle | Five codes share one copy of the bundle in the prompt instead of five copies. |
| Fast profile for narrow tasks | Scoping turns, attestation profiles and narrative prose do not need the reasoner. |
| Small outputs by construction | Two-phase scenario generation and two-phase extraction keep each output well under budget so the truncation ladder is rarely climbed. |
| Output budget tiers (`LLM_BUDGET_SMALL/MEDIUM/LARGE` = 16 384 / 32 768 / 65 536) | Each call requests what its kind needs, including room for hidden reasoning; OpenRouter reserves credit for the full `max_tokens`, so over-asking has a real (reservation, not spend) cost. Lowering any tier below the default is an accuracy regression and is flagged as such. |
| Bounded concurrency (4 for fan-out stages, 2 for whole-bundle batches) | Throughput within provider limits; the lower whole-bundle concurrency is deliberate so later batches see earlier batches' contradictions. |
| Dev-only response cache (`LLM_DEV_CACHE=1`, refused when `APP_ENV=production`) | Re-running an unchanged stage while developing costs nothing; only complete responses are cached (never a `length` cut or a content-filtered body); cache hits are recorded with zero tokens so cost accounting stays honest; the benchmark refuses to measure against a backend with it on. |
| Metered cost per call | `model_call.cost_usd` from OpenRouter's `usage.cost`, so cost per stage and per assessment is a fact, not an estimate (`scripts/backfill_cost.py` estimates only for rows predating metering and labels them so). Azure does not meter: its calls are recorded unmetered (`cost_source = ""`), tokens exact. |

The accuracy programme's closing report (`docs/accuracy-program/phases/06-full-validation.md`)
records the combined effect on six benchmark vendors: bands within one level of expected for
6 / 6 (baseline 0 / 6), duplicates per golden 0.42 (baseline 0.5–1.9), signal share median 78 %
(baseline 27.5 % on the canary), at ≈ 54 % of the baseline token cost.

---

## 5. Large files and variable context windows

The configured models change (any OpenRouter id or Azure deployment can be set) and their context windows and output
caps differ by an order of magnitude. Accuracy must not depend on that. The rule throughout the
pipeline is: **decide the shape of the work from the size of the input, keep every unit of work
well inside the window, never drop evidence to make something fit, and split the input when the
output overflows rather than truncating it.**

Token counts are approximated as `len(text) / 4` (no tokenizer dependency); the thresholds below
are set conservatively enough that the estimate's error does not matter.

### 5.1 Thresholds and units of work

| Constant | Value | Where | Meaning |
|---|---|---|---|
| chunk size | 1 500 characters (≈ 375 tokens); XLSX also ≤ 6 rows | `app/parsing/*` | Citation granularity; chunks never cross a page (PDF) or a heading. |
| `SINGLE_CALL_MAX_TOKENS` | 80 000 | `document_weaknesses.py` | Above this a document is not extracted in one call. |
| `ENUMERATE_INPUT_MAX` | 150 000 | `document_weaknesses.py` | Above this the enumeration pass itself is windowed. |
| `QUESTIONNAIRE_WINDOW_MAX_TOKENS` | 12 000 | `document_weaknesses.py` | Window size for long questionnaires. |
| `PHASE2_CONCURRENCY` | 4 | `document_weaknesses.py` | Parallel per-finding detail calls. |
| `WHOLE_BUNDLE_MAX_TOKENS` | 100 000 | `gap_analysis.py` | Above this gap analysis falls back to retrieval. |
| `BUNDLE_BATCH_SIZE` / `BUNDLE_CONCURRENCY` | 5 / 2 | `gap_analysis.py` | Codes per whole-bundle call; parallel batches. |
| `FTS_TOPK` | 8 (env) | `retrieval.py` | Chunks retrieved per control in fallback mode. |
| `_BATCH_SIZE` | 8 | `cross_correlation.py` | Weaknesses per correlation cluster. |
| `_REASONER_MIN_CONTEXT` / `_FAST_MIN_CONTEXT` | 200 000 / 100 000 | `router.py` | Capability guard: what a model must offer to be safe under the thresholds above. |

The whole-bundle path packs at most ~100 000 input tokens of evidence plus prompts, so the
reasoner guard requires a 200 000-token window: the largest unit of work sits at roughly half the
smallest permitted context, leaving room for prompts, control descriptions, known weaknesses and
the reported-contradictions list.

### 5.2 Extracting weaknesses from one large document

`document_weaknesses.extract()` picks a path from the rendered size of the document:

```
render all chunks with [chunk_id, section_path, page] markers
        │
        ├─ ≤ 80k tokens ──▶ ONE reasoner call over the whole document ──▶ rows
        │                        │ finish_reason == length after the ladder (truncated=True)
        │                        ▼
        └─ > 80k tokens ──▶ adaptive fallback, by document kind
                                │
                ┌───────────────┴────────────────┐
                ▼                                ▼
      questionnaire                     every other kind (SOC, ISO, pen test, policy, other)
      windowed direct extraction        two-phase, structure-aware extraction
      ─────────────────────────         ─────────────────────────────────────
      group chunks by sheet/domain      Phase 1  enumerate skeletons (heading, severity,
      pack into ≤ 12k-token windows              section_path, kind_signal) — small output
      (a section is never split)                 • ≤ 150k tokens: one window
      one ordinary extraction call               • otherwise: windows that never split a
      per window; "report only what                section, each prefixed with the document
      is in this window"                           preamble (~1.5k tokens: exec summary /
      output truncates → split the                 TOC / severity legend) so cross-references
      window in half, recurse down to              and severity calibration stay consistent
      single sections                            skeletons deduped by normalised heading
      nothing is capped                 Phase 2  one detail call per skeleton, scoped to
                                                 that section + the preamble, 4 in parallel,
                                                 each in its own DB session
```

Why it is shaped this way:

- **Whole document when it fits.** The most accurate reading is the one where the model sees the
  entire document, so that is the default and the threshold (80 000) is set well below the point
  where long-context recall degrades, not at the model's ceiling.
- **Sections are atomic.** A finding's heading and body must land in the same window; windows are
  packed from whole sections, and an oversized single section becomes its own window rather than
  being cut.
- **The preamble travels with every window** so that a severity legend defined on page 2 still
  calibrates a finding on page 60.
- **Questionnaires are not enumerated.** Their findings are rows, one per negative answer, and
  can number in the hundreds; a small-output enumeration pass would need a cap. Instead every
  window is extracted directly and nothing is capped: if a dense sheet overflows the output
  budget, the window is halved and both halves extracted, down to single sections. Every row is
  seen exactly once.
- **Overlap is safe.** Rows are inserted one at a time under the unique
  `(assessment_id, dedupe_key)` index (`dedupe_key` = normalised quote + chunk); a finding seen
  twice across adjacent windows or a retried window cannot be stored twice.
- **Nothing is dropped by a finding cap.** Every skeleton is detailed; concurrency bounds load,
  not count. A phase-2 failure is reported per finding and the job fails loudly with the count.

### 5.3 Assessing controls against a large bundle

Gap analysis chooses between two modes by the size of the *whole* parsed bundle (§3.5):

- **≤ 100 000 tokens: whole bundle in context.** Every control verdict is made with all the
  evidence visible, which is what lets contradictions between documents be found in the same call
  and lets "the bundle is silent on this control" be asserted honestly. The unit of work is a batch
  of ≤ 5 control codes; the bundle is repeated per batch, which is why batches exist at all.
- **> 100 000 tokens: per-control retrieval.** The bundle no longer fits safely, so each control
  is assessed on the top-8 BM25 chunks for its code, name, description and scenario, with one
  bounded second retrieval pass when the model proposes better terms. This path is kept as the
  fallback only; the benchmark cases (15–25k tokens each) all run whole-bundle. Per-control mode
  loses cross-document context, so `insufficient_info` in this mode means "none of the retrieved
  candidates speak to it" and the prompt says so.

Confirmation (§3.4) always uses the whole bundle: it is the step whose entire purpose is judging a
candidate against everything supplied. If a bundle were so large that even confirmation could not
fit, it would fall to the same output-splitting rule below; input-side windowing of confirmation
is not implemented today and would be an accuracy trade-off to design.

### 5.4 Output overflow: split the input, never truncate the answer

A model's output cap is the other variable. Every structured stage handles it the same way:

1. The router climbs the budget ladder: start at the call's tier, double on `length`, up to
   `LLM_TRUNCATION_CAP` (128 000) or the model's own output cap, at most twice (§4.2).
2. If it still truncates, the router raises `truncated=True` **without parsing** — a truncated
   JSON list is indistinguishable from a complete short one, so parsing it would silently lose
   findings.
3. The caller splits its **input** and retries each half, recursively, down to the smallest unit:

| Stage | Split unit | Floor |
|---|---|---|
| Questionnaire window extraction | window → halves by section | single section |
| Confirmation | candidates of one document → halves | single candidate (then kept and flagged, never lost) |
| Merge | confirmed rows → halves | 4 rows (cross-half merges are sacrificed: strictly fewer merges, never invented ones) |
| Cross-correlation | cluster of ≤ 8 → halves | single weakness |
| Whole-bundle gap analysis | batch of ≤ 5 codes → halves | single code |
| Two-phase extraction, phase 2 | (nothing to split: one finding) | the ladder is the only remedy; failure is reported per finding |

4. Where no split is possible the failure is persisted where it happened (per control, per
   document, per finding), the phase reports it, and it is resumable without redoing the rest.

The same rule applies to prose: a narrative that hits the cap is retried larger and otherwise
fails; a cut-off paragraph is never persisted.

### 5.5 What this guarantees

- Changing the model changes speed and cost, not which evidence is read: the units of work are
  sized by the code; the startup guard logs an error for a configured model whose output cap is
  below its profile's requirement (reasoner ≥ `LLM_MIN_MODEL_OUTPUT_CAP`, fast ≥ `LLM_BUDGET_MEDIUM`),
  and the truncation ladder clamps at the model's catalogued cap and fails loudly there rather than
  persisting a cut answer.
- Every persisted finding is grounded in a chunk the pipeline actually showed the model, and the
  quote in it can be located again.
- Every count the UI shows (findings extracted, controls assessed, candidates reviewed) is a
  count of completed units; partial work is visible as partial, and failures name their unit.

---

## 6. Liveness, robustness and telemetry of model calls

Completions are **streamed** (SSE) and reassembled into the ordinary response shape. Streaming is
what makes the deadlines liveness-based instead of duration-based: the router sees every token
and keepalive, so a slow reasoning model that is working is never cut off, while a dead
connection is detected within minutes.

| Limit | Default | Meaning |
|---|---|---|
| `LLM_CONNECT_TIMEOUT_S` | 30 | TCP / TLS connect and pool acquisition. |
| `LLM_STREAM_IDLE_S` | 180 | No bytes at all on the socket (keepalive comments count) → dead connection. |
| `LLM_STREAM_IDLE_NO_KEEPALIVE_S` | = `LLM_CONTENT_SILENCE_S` | The same tier for providers that send no keepalives (Azure): the socket is legitimately silent while the model reasons, so this replaces the 180 s read timeout there; a heartbeat pings the task every `TASK_WATCHDOG_INTERVAL_S` meanwhile so the watchdog stays quiet. Validated `LLM_STREAM_IDLE_S ≤ it ≤ LLM_CALL_MAX_S`. |
| `LLM_CONTENT_SILENCE_S` | 3 600 | Socket alive but no output or reasoning token. Equal to the ceiling by default (tier off): models that reason *hidden* look exactly like a stall; streamed reasoning deltas count as activity. |
| `LLM_CALL_MAX_S` | 3 600 | Hard per-attempt ceiling. |
| `TASK_IDLE_TIMEOUT_S` | 600 | Watchdog: task with no activity (must be ≥ stream idle). |
| `TASK_MAX_RUNTIME_S` | 14 400 | Watchdog: task age (must be ≥ call max). |

Retry rules: a transient failure (network error, 408 / 429 / 5xx / 524, provider `finish_reason:
error`, empty stream, a read timeout with nothing received) is retried **only if no output token
has arrived**, up to `LLM_TRANSIENT_RETRIES` (6) times with exponential backoff — 1.5 s doubling,
stretched to the provider's `Retry-After` on a 429 or 503 when it asks for longer, every wait
capped by `LLM_RETRY_AFTER_CAP_S` (60 s), so the worst case is ≈ 95 s. Every retry is logged at
WARNING and recorded on the attempt (`attempts_json[].transport_retries`: status, wait, the
provider's `x-ratelimit-*` headers on a 429) so a lost stage shows what was tried. A 429 storm that
outlasts the loop is a provisioning problem, not a retry one — Azure admits a request by reserving
its `max_completion_tokens` against the deployment's tokens-per-minute, so a stage running
`N` concurrent calls needs `N × budget` TPM (gap analysis: 4 × 65 536 ≈ 262 k); the WARNING prints
the limit headers next to the reserved budget so the remedy (raise the deployment's TPM slider or
lower the stage concurrency) is in the log. After partial output the call fails with
`partial=True` and the received head is kept for diagnosis — never a second bill and never a silent
fallback; the hard ceiling is never retried either. The one deliberate exception is a response the provider's
content filter cut (§4.2 point 5): the body is complete from the transport's point of view but
unusable, so it is retried once with fresh context and then fails with `filtered=True`. Cancelling the task closes the stream. Configuration is
validated at startup (`0 < connect ≤ idle ≤ silence ≤ call max`; watchdog ≥ router limits), and
lowering the liveness limits below the defaults is documented as an accuracy trade-off because a
timed-out stage is a lost assessment step, never a faster one.

Per-call telemetry in `model_call`: purpose, profile, model id, prompt hash, latency, time to
first token, input / output / cached / reasoning tokens, metered cost and its source, success,
error text, how the last attempt finished (`finish_reason`, `native_finish_reason`, `provider`,
`generation_id`), a per-attempt summary (`attempts_json`: layer, requested budget, outcome, finish
reasons, provider, dialect, temperature sent, token split, transport retries), the first 8 000 and last 2 000 characters of output on failure,
and whether the dev cache served it. Every retry and every final failure is also logged at WARNING
(root logging level `LOG_LEVEL`, default `INFO`), and `LLM_FAILURE_DUMP_DIR`, when set, writes the
complete output of every attempt of a failed call to a JSON file. The benchmark reads this table
to attribute cost per stage.

---

## 7. HTTP API summary

All routes are under `/api`. Job-starting routes return a task status and re-attach to an
in-flight run of the same kind.

| Area | Routes |
|---|---|
| Assessments | `GET/POST /assessments`, `GET/DELETE /assessments/{id}`, `GET/POST .../description`, `PATCH .../settings` (analysis date, standards profile), `PATCH .../model-overrides` |
| Scoping | `POST .../scoping/turn`, `POST .../scoping/force-continue` |
| Documents | `GET/POST .../documents`, `DELETE /documents/{id}`, `GET /documents/{id}/chunks`, `GET /chunks/{id}`, `GET /documents/{id}/url` → signed `GET /documents/{id}/file`, `POST /documents/{id}/extract-weaknesses`, `POST /documents/{id}/attestation-profile` |
| Weaknesses | `GET .../weaknesses[?include=all]`, `GET /documents/{id}/weaknesses`, `GET .../findings` (unmatched weaknesses, worst first), `POST .../cross-correlate` (alias `POST .../weaknesses/synthesize`) |
| Scenarios and controls | `GET .../scenarios`, `POST .../scenarios/generate`, `PATCH/DELETE /scenarios/{id}`, `POST/PATCH/DELETE /expected-controls/...`, `PATCH /control-assessments/{id}`, `POST /expected-controls/{id}/assess` (manual verdict), `POST /expected-controls/{id}/assess-ai` |
| Gap analysis | `POST .../gap-analysis/run[?only_failed=true]` |
| Scoring and narrative | `POST .../recalculate`, `POST .../narratives/run`, `POST .../executive-summary/run`, `GET .../report` |
| Models and tasks | `GET /models`, `GET /tasks/{id}`, `POST /tasks/{id}/cancel`, `GET /tasks/{id}/events` (SSE) |
| Health | `GET /health` → `{ok, app_env, llm_dev_cache}` |

Interactive documentation is served by FastAPI at `/docs` on the backend port.

---

## 8. Data model

| Table | Holds |
|---|---|
| `assessment` | Vendor name, `phase_state` (per-step started / completed / error / stale stamps), `as_of_date`, `standards_profile`, `model_overrides`, `executive_summary` blob with fingerprint. |
| `service_description`, `description_turn` | The scoped description, sufficiency breakdown and the Q&A transcript. |
| `document`, `chunk`, `chunk_fts` | Uploaded files (kind, sha256, parse and extraction stamps, extraction task id and error, attestation profile and its error) and their citation-ready chunks; FTS5 shadow table maintained by triggers. |
| `scenario`, `expected_control` | Inherent-risk scenarios (`source` = description or emergent_from_weakness, `origin_weakness_ids`) and the controls each expects (code, weight, rationale). |
| `control_assessment`, `control_evidence` | The verdict per expected control (coverage, effectiveness, rationale, user lock, unresolved citations, last error / run) and its bound citations (chunk, polarity, quote). |
| `weakness` | Every finding: source chunk and document, severity, description, quote, `kind_signal`, `mapped_control_codes`, `unmatched`, `origin` (document / gap_analysis / attestation_check), `status` (candidate / confirmed / evidence_note / dropped / merged), `review` (decision, reason, confidence, evidence strength, merge trail), `evidence_refs`, `dedupe_key` (unique per assessment). |
| `meta_issue` | Evidence-quality flags per control (`insufficient_info`, `vague_answer`, `missing_doc`) and `unscored_finding` audit rows. |
| `model_call` | One row per logical LLM call, with `attempts_json` itemising its attempts (§6). |
| `llm_cache` | Dev-only response cache. |
| `task` | Durable mirror of the job registry with `last_activity_at` and structured `stats`. |

Schema changes are additive `ALTER TABLE … ADD COLUMN` statements applied at startup; there is no
Alembic. New databases are created with `Base.metadata.create_all`.

---

## 9. Testing strategy

- `backend/tests/` (233 tests): unit tests for parsing, scoring, the router's truncation /
  garble / streaming / liveness behaviour, the provider dialects (model refs, Azure request shape
  and stream reassembly, its content filter, `Retry-After`, the no-keepalive tier and heartbeat,
  unmetered cost, `temp=fixed`), workflow guards and stale stamps, task progress, the
  attestation checks, each accuracy-programme phase, and an end-to-end walk of the whole API with
  a fake model returning canned JSON (`test_e2e.py`). Two tests assert that the frontend's copies
  of the risk matrix and the settings stage list match the backend.
- `benchmark/tests/` (73 tests): the harness's own logic (case loading, HTTP client, metrics,
  judge parsing, pricing, dashboard view-model, an integration run against a fake app).
- Accuracy itself is measured by the benchmark against hand-curated golden cases; see
  `benchmark/README.md` and `docs/accuracy-program/TESTING.md` for the cost-aware protocol
  (one canary vendor during development, stage-only re-runs on stored assessments, LLM judge only
  at gates).
