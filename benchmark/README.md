# TPRM Benchmark

Automated accuracy benchmark for the TPRM assessment app. It feeds prepared
inputs (vendor description + evidence documents) into the main app, runs the
full assessment pipeline autonomously over HTTP, grades the resulting report
against hand-authored golden expectations with an LLM judge, and tracks
results across app versions and model configurations in a graphical dashboard.

**Fully separate from the main app**: no shared code (never imports
`backend/app/*`), HTTP-only integration, its own virtualenv and its own SQLite
results DB (`data/bench.sqlite`).

---

## Contents

1. [Architecture](#architecture)
2. [Setup](#setup)
3. [Running a benchmark](#running-a-benchmark)
4. [CLI reference](#cli-reference)
5. [Configuration reference](#configuration-reference)
6. [Case file syntax](#case-file-syntax)
7. [How grading works](#how-grading-works)
8. [What is recorded per run](#what-is-recorded-per-run)
9. [Results database schema](#results-database-schema)
10. [Dashboard](#dashboard)
11. [Authoring good cases](#authoring-good-cases)
12. [Tests](#tests)
13. [Troubleshooting](#troubleshooting)

---

## Architecture

```
┌─────────────────────────── benchmark/ ───────────────────────────┐
│                                                                  │
│  bench (CLI)                                 dashboard (:8100)   │
│  ┌────────────┐    ┌───────────┐             ┌────────────────┐  │
│  │ cases.py   │──▶ │ runner.py │──results──▶ │ FastAPI+Jinja2 │  │
│  │ YAML+docs  │    │ batch     │    ▲        │ + Chart.js     │  │
│  └────────────┘    │ driver    │    │        └────────────────┘  │
│                    └─────┬─────┘ data/bench.sqlite               │
│                          │           ▲                           │
│              ┌───────────┴─────┐     │                           │
│              ▼                 ▼     │                           │
│      app_client.py         judge.py──┘                           │
│      (HTTP driver)         (LLM judge)                           │
└─────────────┼──────────────────┼─────────────────────────────────┘
              │ HTTP             │ HTTPS
              ▼                  ▼
   TPRM backend (:8000/:8010)  OpenRouter /chat/completions
   the app under test          pinned judge model, temp 0
```

Module map (`bench/`):

| Module | Responsibility |
|---|---|
| `config.py` | All settings (`BenchSettings`, env / `.env` overridable) |
| `cases.py` | Case schema (pydantic) + YAML loader/validation |
| `app_client.py` | Typed HTTP client for the app under test + `wait_task` polling (with durable-phase fallback) |
| `runner.py` | Per-case pipeline orchestration, grading, persistence, batch driver |
| `judge.py` | OpenRouter judge client, output schemas, structural checks, one validation-driven retry |
| `prompts.py` | Judge prompt templates; each carries a `PROMPT_VERSION` |
| `metrics.py` | Pure numeric aggregation (P/R/F1, severity agreement, rubric weights) — no I/O, no LLM |
| `models.py` / `db.py` | SQLAlchemy schema + engine for `data/bench.sqlite` |
| `versioning.py` | Provenance: main-repo git SHA (+dirty), backend `pyproject` version, model snapshot |
| `collect.py` | Optional read-only token/cost collection from the app's `model_call` table |
| `cli.py` | `bench run` / `bench list-cases` / `bench init-db` |

Design rules the code enforces:

- **The judge never emits a final score.** It only produces match/rubric
  structures with per-item justifications; every number is derived
  deterministically in `metrics.py`. This keeps runs comparable across judge
  prompt versions (which are recorded per run).
- **A failing case never kills the batch.** Any stage error or judge failure
  is recorded on that `case_result` (`status`, `error_stage`, `error_detail`)
  and the batch continues. The process exit code is non-zero if anything
  failed (CI-friendly), but the run row is always written.
- **Stage order: scenarios before documents.** The app's cross-correlation
  step maps extracted weaknesses onto *existing scenarios'* expected controls
  (see `backend/app/ai/agents/cross_correlation.py`), so the runner generates
  scenarios first, then uploads documents (awaiting each auto-fired
  extraction), then runs an explicit awaited cross-correlate pass
  (idempotent — the auto-fired one exposes no task id to poll).
- **Async completion detection.** Background stages return a `task_id`; the
  runner polls `GET /api/tasks/{id}` until `done|error` with per-stage
  timeouts. If the task registry lost the task (backend restart → 404), it
  falls back once to the durable `phases` state on
  `GET /api/assessments/{id}`.

## Setup

```bash
cd benchmark
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

Create `benchmark/.env` (or export env vars):

```bash
OPENROUTER_API_KEY=sk-or-...          # required — the judge calls OpenRouter directly
BENCH_BACKEND_URL=http://localhost:8000
JUDGE_MODEL=anthropic/claude-sonnet-4.6
MAIN_DB_PATH=/abs/path/to/backend/data/tprm.sqlite   # optional, token/cost collection
```

## Running a benchmark

Start the main backend first — **a single instance** (its background-task
registry is in-process; tasks die on restart):

```bash
cd ../backend && .venv/bin/uvicorn app.main:app --port 8000
```

Then, from `benchmark/`:

```bash
.venv/bin/bench list-cases                    # sanity: cases load and validate
.venv/bin/bench run --smoke                   # cheap end-to-end check (~cents)
.venv/bin/bench run                           # all cases, app default models
.venv/bin/bench run --cases meridian --reps 3 # subset, repeated
.venv/bin/bench run \
    --override scenarios=anthropic/claude-opus-4.7 \
    --notes "opus on scenario generation"     # A/B a model change
```

Watch results at any time in the dashboard (see below) — runs appear as soon
as they start; per-case rows appear as they finish.

## CLI reference

### `bench run`

| Flag | Default | Meaning |
|---|---|---|
**Grading a stored assessment (no pipeline, no app LLM cost):**

```
.venv/bin/bench grade --assessment 9 --case orbitclear [--judge match|full|none] [--notes "..."]
```
Fetches the report (+ chunks for `--judge full`) of an assessment that already exists on the backend and runs the judge
only. Records a run with `config.mode = "grade"`, empty stage timings, and `tokens_json` reflecting the stored
assessment's original pipeline cost. Use it to re-grade after re-running a single stage on a stored assessment
(`POST .../extract-weaknesses`, `.../gap-analysis/run`, `.../recalculate`) instead of driving the whole pipeline.

| `--cases a,b` | all | Comma-separated case ids (directory names under `cases/`) |
| `--reps N` | 1 | Repetitions per case (distinct assessments; measures nondeterminism) |
| `--concurrency N` | 1 | Parallel cases via threads. **Experimental above 1** — the backend task registry is in-process and the app DB is SQLite (write-lock contention); keep N ≤ 3 |
| `--cleanup none\|ok\|all` | `ok` | Delete created assessments after grading. `ok` keeps errored ones for debugging |
| `--judge none\|match\|full` | `full` | `none`: no judge calls — deterministic metrics only (`band_error`, `n_weaknesses`). `match`: weakness matching (P/R/F1, ~5k tokens). `full`: matching + signal/noise classification of every reported weakness against the evidence chunks + exec-summary rubric |
| `--skip-narratives` | off | Skip the narratives + executive-summary stage (~30k app tokens); the exec rubric is then not graded |
| `--judge-model M` | `JUDGE_MODEL` | Judge model for this run (recorded on the run) |
| `--override STAGE=MODEL` | — | Per-stage model override, repeatable. Stages: `scoping`, `scenarios`, `gap_analysis`, `weaknesses`, `narrative`, `executive_summary` |
| `--smoke` | off | Pins all six stages **and** the judge to the app's fast-profile default — a cheap plumbing check, not an accuracy measurement. Explicit `--override`/`--judge-model` still win |
| `--notes "..."` | — | Free-text note stored on the run |

Exit code: `0` when every case graded ok, non-zero otherwise.

### `bench list-cases`

Loads and validates every case, prints a one-line summary each. Fails with a
specific error if any case is invalid (missing docs, bad severity, duplicate
golden ids, too-short description).

### `bench init-db`

Creates `data/bench.sqlite` with the schema. Optional — the runner does this
automatically on first use.

## Configuration reference

All settings live in `bench/config.py` (`BenchSettings`) and can be set via
environment variables or `benchmark/.env`:

| Variable | Default | Meaning |
|---|---|---|
| `BENCH_BACKEND_URL` | `http://localhost:8000` | The app under test |
| `OPENROUTER_API_KEY` | — | **Required.** Judge auth (independent of the app's key handling) |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | Judge endpoint |
| `JUDGE_MODEL` | `anthropic/claude-sonnet-4.6` | Pinned judge model |
| `JUDGE_MAX_TOKENS` | 32768 | Judge completion cap — sized for 100+ reported findings (one justified entry each in the match and classification steps) |
| `JUDGE_CLASSIFY_CHUNK_BUDGET_CHARS` | 120000 | `--judge full`: the whole evidence bundle (all chunks) is sent to the classifier when it fits this many characters; otherwise only the chunks the weaknesses cite |
| `BENCH_DB_PATH` | `benchmark/data/bench.sqlite` | Results DB |
| `CASES_DIR` | `benchmark/cases` | Case directory root |
| `MAIN_REPO_DIR` | repo root | Where `git rev-parse` runs for provenance |
| `MAIN_DB_PATH` | `<repo>/data/tprm.sqlite` | App DB for read-only token/cost collection; unset/missing → collection silently skipped |
| `TIMEOUT_SCENARIOS` | 600 s | Stage timeout |
| `TIMEOUT_EXTRACTION` | 600 s | Per-document extraction timeout |
| `TIMEOUT_CORRELATE` | 600 s | Cross-correlation timeout |
| `TIMEOUT_GAP_ANALYSIS` | 1800 s | Gap analysis timeout (longest stage) |
| `TIMEOUT_NARRATIVES` | 900 s | Narratives + exec summary timeout |
| `POLL_INTERVAL` | 2.0 s | Task polling interval |
| `MATCH_CONFIDENCE_THRESHOLD` | `high,medium` | Judge confidences that count a match as a true positive |

Provenance caveat: the git SHA is read from `MAIN_REPO_DIR`, which assumes the
backend at `BENCH_BACKEND_URL` is the co-located checkout. The `backend_url`
is recorded on every run so a mismatch is auditable.

## Case file syntax

A case is a directory under `cases/` containing `case.yaml` and a `docs/`
folder. Full annotated example:

```yaml
id: globaltalent                     # must match the directory name usage in --cases
vendor_name: "GlobalTalent HR Solutions LLC"

# The runner skips the interactive scoping Q&A via force-continue, so this
# description must be self-sufficient. Cover all seven scoping dimensions:
# data types, hosting, network access, identity flow, regulatory scope,
# geography, criticality. Minimum 10 chars (API limit); aim for much more.
description: |
  GlobalTalent is a cloud-hosted (SaaS) HR information system used for ...

documents:                           # uploaded in listed order
  - path: docs/SIG_Lite_Response.xlsx      # relative to the case directory
    kind: questionnaire                    # questionnaire|soc|iso|pentest|policy|other
  - path: docs/SOC_Report.pdf              # PDF, XLSX and DOCX are supported
    kind: soc

golden:
  # The answer key: weaknesses the app SHOULD report. Hand-curated judgments
  # anchored in the documents — never blind copies of app output.
  expected_weaknesses:
    - id: GT-W1                      # unique across the whole golden section
      description: >-
        The SOC report carries a qualified opinion: the auditor identified
        material weaknesses in multiple control areas.
      severity: high                 # low|medium|high|critical (graded for agreement)
      mapped_control_codes: [IAM.MFA]  # optional, informational for the judge
      optional: false                # true = "nice to find": missing it is not a
                                     # false negative, matching it is not a true
                                     # positive, and its match is never a false
                                     # positive. Use for debatable items.

  # Rubric for the executive summary.
  exec_summary_rubric:
    must_cover:                      # points the summary must substantively address
      - id: GT-K1
        point: >-
          An overall residual risk verdict is stated and is consistent with
          the computed aggregate band.
    must_not_claim:                  # claims the summary must NOT assert
      - id: GT-F1                    # (hedged/negated mentions do not count)
        claim: The SOC report opinion was unqualified or "clean".
```

Validation (at load time, before anything runs): document files exist, `kind`
and `severity` are in their allowed sets, golden ids are unique, description
meets the API minimum. `bench list-cases` runs exactly this validation.

## How grading works

Both graders receive the canonical `GET /api/assessments/{id}/report` payload
— the same artifact a human reviews.

**1. Weakness matching → precision / recall / F1**

The judge receives the golden list and the app-reported weaknesses as JSON and
must place *every* id on exactly one of three lists — `matches` (with a
confidence and a justification quoting the decisive phrase from both sides),
`unmatched_expected`, `unmatched_actual`. A match requires the *same
underlying deficiency*, not the same topic area. Structural violations
(missing/duplicated ids) are rejected and retried once with the error appended.

Scoring (`metrics.py`):

- A match counts as **TP** only when its confidence meets
  `MATCH_CONFIDENCE_THRESHOLD` (`high`/`medium` by default). Below-threshold
  matches degrade to FN + FP.
- Golden items with `optional: true` never contribute to TP or FN, and their
  matched actuals are not FPs.
- `precision = TP/(TP+FP)`, `recall = TP/(TP+FN)`, `F1 = 2PR/(P+R)`.
- Severity agreement over counted matches, on an ordinal scale
  (low=1…critical=4): `severity_exact` (fraction equal) and `severity_mae`
  (mean absolute rank difference).

**1b. Signal/noise classification (`--judge full`)**

A second judge call classifies EVERY reported weakness against the parsed
evidence chunks (the only source of truth about what the documents say) into
one category from `testdata/_results/cases/fp_spec.md`: `TP`, `TP_OPTIONAL`,
`DUP_OF_TP`, `LEGIT_UNKEYED`, `BOILERPLATE`, `MISREAD`, `JUDGE_FN` (matcher
missed a real golden hit), `JUDGE_FP_MATCH` (matcher linked a non-hit). It also
states, for each missed golden, whether the underlying fact is present in any
chunk (ingestion vs reasoning problem). Structural checks: every reported id
exactly once, golden required/forbidden per category, at most one primary hit
per golden. Metrics (`metrics.score_classification`):

- `signal_share = (TP + TP_OPTIONAL + LEGIT_UNKEYED + JUDGE_FN) / n reported`
- `dup_per_golden = DUP_OF_TP / distinct goldens with a primary hit`
- `judge_fn` count — a non-zero value means the P/R/F1 above under-count.

P/R/F1 are deliberately NOT corrected by the classification so they stay
comparable with pre-Phase-0 runs.

**1c. Deterministic metrics (any `--judge` mode)**

- `band_error = rank(app aggregate band) − rank(golden expected_band)` on
  Low=1 … VeryHigh=4 (+ = app harsher); `None` when the case has no `expected_band`.
- `n_weaknesses` = weaknesses reported.

**2. Executive summary rubric → 0–100**

The judge grades the flattened summary against: the `must_cover` points
(`covered` / `partial` / `missing` / `unknown`), the `must_not_claim` claims
(`violated` / `clean` / `unknown`; hedged or negated mentions are not
violations), and **faithfulness (0–5) against an evidence digest built from
the report itself** (aggregate band, scenarios, weaknesses, meta issues) — so
the summary is judged against what the assessment actually found, not the
judge's world knowledge. Unsupported claims are listed.

Aggregation: `coverage` scores covered=1, partial/unknown=0.5, missing=0;
`violation = 1 − violated/n` (empty list → 1.0); `faithfulness = score/5`;

```
exec_overall = 100 · (0.5·coverage + 0.3·faithfulness + 0.2·violation)
```

Weights are constants in `metrics.py` and recorded in each run's
`config_json`.

**Auditability.** Every judge invocation — including failed attempts — is
persisted verbatim (`judge_call`: full request messages, raw response, model,
prompt version, tokens, latency, error). The dashboard case view exposes them
under "Judge calls (raw, for audit)".

## What is recorded per run

| Field | Source |
|---|---|
| `app_git_sha` + dirty flag | `git rev-parse HEAD` / `git status --porcelain` in `MAIN_REPO_DIR` |
| `app_version` | `backend/pyproject.toml` |
| `models_json` | The overrides the runner set **plus** a snapshot of `GET /api/models` (so un-overridden stages stay attributable) |
| `judge_model`, `judge_prompt_versions_json` | Runner config + `prompts.py` versions |
| `config_json` | Full CLI config incl. confidence threshold and exec weights |
| Per case: `timings_json` | Wall-clock seconds per stage |
| Per case: `tokens_json` | Best-effort read-only aggregation of the app's `model_call` rows (calls, tokens, cost, errors by purpose/model) |
| Per case: `report_json` | Full `ReportOut` snapshot — the graded artifact, kept for re-inspection |

## Results database schema

`data/bench.sqlite` (SQLAlchemy models in `bench/models.py`):

- **`run`** — one row per `bench run`: timestamps, status
  (`running|done|partial|failed`), backend URL, app SHA/version, model config,
  judge model + prompt versions, full config, notes.
- **`case_result`** — one row per (case, repetition): status
  (`ok|error|judge_error`), error stage/detail, assessment id (+ whether it
  was deleted), timings, aggregate band/rank, TP/FP/FN,
  precision/recall/F1, severity agreement, exec subscores + overall, tokens,
  report snapshot.
- **`finding_match`** — one row per judged finding:
  `matched|missed|extra`, both descriptions/severities, confidence, whether it
  counted toward the metrics, and the judge's justification.
- **`judge_call`** — one row per judge attempt: purpose
  (`weakness_match|exec_rubric`), model, prompt version, latency, tokens,
  ok/error, full request and raw response JSON.

## Dashboard

```bash
.venv/bin/uvicorn dashboard.app:app --port 8100
```

Read-only over `data/bench.sqlite`. Chart.js is vendored
(`dashboard/static/vendor/`) — no CDN, no build step; light/dark theme follows
the OS.

| Route | Shows |
|---|---|
| `/` | Runs table + trend lines: per-case F1 and exec score across runs (tooltip shows app SHA + model config per point) |
| `/runs/{id}` | Run metadata, per-case metric table, grouped bar charts (P/R/F1 and exec subscores per case) |
| `/runs/{id}/cases/{crid}` | Matched / missed / extra findings with judge justifications; exec rubric breakdown; the app's exec summary; stage timings; model usage; raw judge I/O |
| `/compare?a=X&b=Y` | Run metadata diff, per-case metric deltas (color-coded), newly-missed / no-longer-missed / newly-hallucinated findings between the two runs |
| `/api/runs`, `/api/runs/{id}` | JSON for programmatic use |

## Authoring good cases

- Golden `expected_weaknesses` are **hand-curated judgments** anchored in the
  documents. Bootstrap workflow: read the docs and note deliberate
  deficiencies → run the pipeline once → review what the app reported →
  curate the golden list from the union. Never blind-copy app output.
- Mark genuinely debatable items `optional: true` rather than arguing with
  the metric.
- After each run, review the **extra findings** table: a defensible extra may
  deserve promotion into the golden set (it is currently penalizing precision
  unfairly).
- Changing a case changes what its historical scores mean. Treat case edits
  like code changes — commit them with a note, and prefer adding a new case
  over silently rewriting one with recorded history.
- Aim for 5–12 expected weaknesses, 4–6 must-cover points, 2–3 forbidden
  claims per case.

## Tests

```bash
.venv/bin/python -m pytest tests/ -q
```

No network, no LLM cost:

- `test_metrics.py` — scoring edge cases (empty golden, all-missed,
  low-confidence degradation, optional handling, severity MAE, rubric math).
- `test_cases.py` — loader validation paths.
- `test_judge.py` — JSON/fence parsing, structural-check retry, failure after
  two bad answers (records preserved), via respx-mocked OpenRouter.
- `test_app_client.py` — `wait_task` state machine (done, error, timeout,
  404 → durable-phase fallback).
- `test_runner_integration.py` — full batch against a stubbed backend +
  stubbed judge: endpoint ordering (scenarios before documents), grading,
  persistence, cleanup policy, and error recording.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `backend at ... is not healthy` | Backend not running, or wrong `BENCH_BACKEND_URL` (check `GET /api/health`) |
| Case errors with `OpenRouter 404 ... deprecated` in a *pipeline* stage | The **app's** configured model (`.env` `MODEL_FAST`/`MODEL_REASONER`) is deprecated on OpenRouter — fix the app config, or pin per-run `--override`s |
| Case errors with `OpenRouter 403 ... Terms Of Service` | The OpenRouter key is blocked for that model/provider — pick a model the key can use (probe with a direct `curl` to `/chat/completions`) |
| `status=judge_error` | Pipeline succeeded but the judge failed twice (parse/structural). The report snapshot is kept; failed judge attempts are in `judge_call` for diagnosis |
| Task 404 mid-run | Backend restarted (in-process task registry). The runner falls back to the durable `phases` state once; keep a single stable backend during a batch |
| No tokens/cost on case results | `MAIN_DB_PATH` unset or unreadable — collection is best-effort and optional |
| Stage timeouts on slow models | Raise `TIMEOUT_*` env vars; gap analysis and per-document extraction are the long poles |
