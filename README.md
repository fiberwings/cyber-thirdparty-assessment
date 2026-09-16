# Cyber Third-Party Risk Assessment

An AI-assisted, evidence-cited cyber risk assessment of a vendor service. You describe the
service, the app proposes the inherent risk scenarios and the controls each one expects, you upload
the vendor's evidence, and the pipeline extracts, confirms, correlates and scores every finding
against that evidence — with a verbatim quote and location for every claim, a deterministic score
you can audit, and an executive summary that only references things that exist.

Accuracy outranks speed and cost: see [`CLAUDE.md`](CLAUDE.md) for the rules every change follows
and [`docs/accuracy-program/`](docs/accuracy-program/) for how the current pipeline was measured
into shape.

## The workflow

Six sequential steps, enforced by the backend (an out-of-order action is refused with the reason,
and changing an input marks everything downstream stale until it is re-run):

| # | Step | What happens |
|---|---|---|
| 1 | **Scoping** | Describe the service; the AI asks one question at a time until seven dimensions (data, hosting, network, identity, regulation, geography, criticality) are covered — or force-continue. |
| 2 | **Inherent risk** | The reasoner proposes risk scenarios (impact × likelihood) and the controls each expects, from a 47-control catalogue. |
| 3 | **Evidence** | Upload questionnaires, SOC 2 / ISO 27001 reports, pen tests, policies (PDF, XLSX, DOCX). Each is chunked with page / section metadata, indexed, and mined for candidate weaknesses; assurance documents also get a typed attestation profile. |
| 4 | **Analysis** | (a) Cross-correlate: deterministic attestation checks, then every candidate is confirmed, noted or dropped against the *whole* bundle, duplicates merged, findings mapped to scenario controls, emergent scenarios spawned. (b) Gap analysis: every expected control gets a coverage / effectiveness verdict with citations and cross-document contradictions. (c) Narratives and executive summary. |
| 5 | **Residual score** | Deterministic 4×4 scoring — no model in the score path — with a residual-risk matrix, register and per-scenario explanation. Edit any verdict and the band updates instantly. |
| 6 | **Report** | Printable report: verdict, key risks, actions, limitations, scenarios, weaknesses, documents. |

Per-assessment **Settings** hold the analysis date, the assessor's own standards profile (required
attestations, freshness windows, residency, MFA policy, SLAs) and per-stage model routing.

## Quick start

Requirements: an [OpenRouter](https://openrouter.ai) API key and either Docker, or Python 3.11+
and Node 22. Full details in [`docs/BUILD.md`](docs/BUILD.md).

**Docker**

```bash
cp .env.example .env         # set OPENROUTER_API_KEY
docker compose up --build    # UI on http://localhost:3000, API docs on http://localhost:8000/docs
```

**Local**

```bash
# terminal 1 — backend
cd backend && python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
cp ../.env.example ../.env   # set OPENROUTER_API_KEY
.venv/bin/uvicorn app.main:app --reload --port 8000

# terminal 2 — frontend
cd frontend && npm ci && npm run dev     # http://localhost:3000
```

Tests (no network, fake model):

```bash
cd backend && .venv/bin/pytest -q      # 194 tests
cd benchmark && .venv/bin/pytest -q    # 73 tests (after `pip install -e ".[dev]"` in benchmark/)
```

## Stack

FastAPI + SQLAlchemy + SQLite (WAL, FTS5) backend · Next.js 15 / React 19 / Tailwind / TanStack
Query frontend · OpenRouter for interchangeable models (defaults: Claude Haiku 4.5 for the `fast`
profile, Claude Opus 4.7 for the `reasoner` profile; any OpenRouter id can be configured or
selected per stage) · a separate `benchmark/` package that drives the app over HTTP and grades it
against hand-curated golden cases.

## Documentation

| Document | Read it for |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Components, the six phases and their enforcement, every AI stage, how accuracy is protected and cost kept down, how large files are handled independently of the model's context window, scoring, liveness, API and data model. |
| [`docs/BUILD.md`](docs/BUILD.md) | Dependencies with versions, step-by-step build and run (Docker, local, benchmark), configuration reference, troubleshooting. |
| [`benchmark/README.md`](benchmark/README.md) | The accuracy benchmark: cases, CLI, judge, metrics, dashboard. |
| [`docs/accuracy-program/STATE.md`](docs/accuracy-program/STATE.md) | Living log of the accuracy programme (plan, baseline, per-phase reports, open decisions). |
| [`CLAUDE.md`](CLAUDE.md) | The accuracy-first rules for changing this codebase. |

## Project layout

```
backend/     FastAPI app: API, AI agents and prompts, parsing, scoring, tests
frontend/    Next.js UI
benchmark/   accuracy harness (own venv, HTTP-only; never imports backend code)
docs/        architecture, build guide, accuracy programme records
data/        SQLite database (git-ignored)      storage/  uploaded evidence (git-ignored)
```
