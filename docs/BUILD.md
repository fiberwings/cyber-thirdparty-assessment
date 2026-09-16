# Building and running from source

This guide lists every dependency with its version, and walks through building and running the
three parts of the repository — the backend, the frontend and the benchmark harness — either with
Docker Compose or directly on a workstation. The steps were verified on the versions in the
"verified" columns below on 2026-09-08.

For what the system does and how it is put together, read [`ARCHITECTURE.md`](ARCHITECTURE.md).

---

## 1. Prerequisites

| Requirement | Minimum | Verified with | Notes |
|---|---|---|---|
| Git | any recent | – | The repository is the build input; there are no release archives. |
| Python | **3.11+** (`requires-python` in both `pyproject.toml` files) | 3.13.11 | Docker image uses `python:3.13-slim`. `venv` and `pip` must be available. |
| SQLite with FTS5 | bundled with Python's `sqlite3` module | 3.51.0 | The evidence index is an FTS5 virtual table. Every mainstream Python build ships FTS5; verify with the command in §7 if in doubt. |
| Node.js | 18.18+ (Next.js 15 requirement) | 22.22.1 | Docker image uses `node:22-slim`. Use Node 22 LTS. |
| npm | 9+ | 10.9.4 | `npm ci` needs the committed `package-lock.json`. |
| Docker Engine + Compose v2 | optional | 29.5.2 / v5.1.4 | Only for the container route (§4). |
| OpenRouter API key | required for any AI call | – | https://openrouter.ai. Keep a comfortable credit balance: OpenRouter reserves credit for each in-flight call's full `max_tokens`, and several stages run four calls in parallel. |
| Network access | to `openrouter.ai` from the backend | – | The frontend never talks to OpenRouter; the benchmark's judge does. |

Platform: developed and verified on Linux (aarch64 and x86_64 both work since every dependency is
pure Python or ships wheels). macOS works the same way. Windows is untested; use WSL 2.

---

## 2. Repository layout

```
.
├── backend/            FastAPI app (Python) — API, AI pipeline, scoring, tests
│   ├── app/            package `app`
│   ├── scripts/        smoke_openrouter.py, backfill_cost.py
│   ├── tests/          pytest suite (fake LLM; no network)
│   ├── pyproject.toml  dependencies (no lock file; version ranges)
│   └── Dockerfile
├── frontend/           Next.js 15 app (TypeScript)
│   ├── app/ components/ lib/
│   ├── package.json + package-lock.json (exact versions)
│   └── Dockerfile
├── benchmark/          separate Python package: `bench` CLI + dashboard
│   ├── bench/ dashboard/ cases/ tests/
│   └── pyproject.toml
├── docs/               this guide, ARCHITECTURE.md, accuracy-program/
├── docker-compose.yml  two services: backend (:8000) and frontend (:3000)
├── .env.example        every setting with its default — copy to .env
├── data/               SQLite database (created at first start; git-ignored)
├── storage/            uploaded evidence, content-addressed (git-ignored)
└── testdata/           local vendor evidence packs (git-ignored; not needed to build)
```

---

## 3. Dependencies and versions

### 3.1 Backend (`backend/pyproject.toml`)

The backend declares version *ranges* and has no lock file; `pip` resolves the newest compatible
release at install time. The "verified" column is what a fresh `pip install -e ".[dev]"` resolved
to on 2026-09-08 and what the test suite was run against. Pin these in a constraints file if you
need reproducible installs.

| Package | Declared | Verified | Role |
|---|---|---|---|
| fastapi | ≥ 0.115 | 0.136.1 | HTTP API |
| uvicorn[standard] | ≥ 0.32 | 0.46.0 | ASGI server |
| sqlalchemy | ≥ 2.0 | 2.0.49 | ORM over SQLite |
| pydantic | ≥ 2.9 | 2.13.3 | Schemas for API and model output |
| pydantic-settings | ≥ 2.6 | 2.14.0 | `.env` / environment configuration |
| httpx | ≥ 0.27 | 0.28.1 | Streaming client for OpenRouter |
| python-multipart | ≥ 0.0.20 | 0.0.27 | File uploads |
| PyMuPDF | ≥ 1.24 | 1.27.2.3 | PDF parsing (imported as `pymupdf`) |
| openpyxl | ≥ 3.1 | 3.1.5 | XLSX parsing |
| pandas | ≥ 2.2 | 3.0.2 | Declared in `pyproject.toml`; not imported by the app today |
| python-docx | ≥ 1.1 | 1.2.0 | DOCX parsing |
| anyio | ≥ 4.6 | 4.13.0 | Async primitives |
| sse-starlette | ≥ 2.1 | 3.4.1 | Server-sent events for `/api/tasks/{id}/events` |
| *dev:* pytest | ≥ 8.3 | 9.0.3 | Test runner (`asyncio_mode = auto`) |
| *dev:* pytest-asyncio | ≥ 0.24 | 1.3.0 | Async tests |
| *dev:* respx | ≥ 0.21 | 0.23.1 | httpx mocking |

Build backend: `setuptools ≥ 68` (declared in `[build-system]`; installed automatically by pip).

### 3.2 Frontend (`frontend/package.json`, resolved by `package-lock.json`)

| Package | Declared | Locked | Role |
|---|---|---|---|
| next | ^15.1.0 | 15.5.15 | Framework (App Router) |
| react / react-dom | ^19.0.0 | 19.2.5 | UI |
| typescript | ^5.7.0 | 5.9.3 | Type checking (`strict`) |
| tailwindcss | ^3.4.16 | 3.4.19 | Styling |
| postcss / autoprefixer | ^8.4.49 / ^10.4.20 | 8.5.13 / 10.5.0 | CSS pipeline |
| @tanstack/react-query | ^5.59.0 | 5.100.8 | Data fetching and polling |
| @radix-ui/react-tooltip | ^1.2.16 | 1.2.16 | Tooltips (risk matrix) |
| lucide-react | ^1.39.0 | 1.39.0 | Icons |
| clsx | ^2.1.1 | – | Class names |
| @types/node, @types/react, @types/react-dom | ^22.10.0 / ^19.0.0 / ^19.0.0 | – | Types |

Use `npm ci` (not `npm install`) to get exactly the locked versions.

### 3.3 Benchmark (`benchmark/pyproject.toml`)

| Package | Declared | Verified | Role |
|---|---|---|---|
| httpx | ≥ 0.27 | 0.28.1 | Drives the app and calls the judge |
| pydantic / pydantic-settings | ≥ 2.9 / ≥ 2.6 | 2.13.4 / 2.14.2 | Case schema, settings |
| PyYAML | ≥ 6.0 | 6.0.3 | `case.yaml` |
| sqlalchemy | ≥ 2.0 | 2.0.51 | Results DB (`benchmark/data/bench.sqlite`) |
| fastapi / uvicorn[standard] | ≥ 0.115 / ≥ 0.32 | 0.139.0 / 0.50.2 | Dashboard |
| jinja2 | ≥ 3.1 | 3.1.6 | Dashboard templates (Chart.js is vendored, no CDN) |
| *dev:* pytest, respx | ≥ 8.3 / ≥ 0.21 | 9.1.1 / 0.23.1 | Tests |

The benchmark never imports backend code; it needs its own virtual environment.

### 3.4 Container base images

| Image | Used by | Extra system packages |
|---|---|---|
| `python:3.13-slim` | `backend/Dockerfile` | `build-essential`, `libxml2`, `libxslt1.1` |
| `node:22-slim` | `frontend/Dockerfile` (deps, builder, runner stages) | none |

---

## 4. Route A — Docker Compose (recommended for running the app)

```bash
git clone <repository-url> cyber-thirdparty-assessment
cd cyber-thirdparty-assessment
cp .env.example .env
# edit .env: set OPENROUTER_API_KEY (and optionally MODEL_FAST / MODEL_REASONER)
docker compose up --build
```

Then open http://localhost:3000. The backend API and its interactive docs are at
http://localhost:8000/docs.

What `docker compose up --build` does:

1. Builds the backend image: installs the package in editable mode from `pyproject.toml`, copies
   `app/`, exposes 8000, runs `uvicorn app.main:app --host 0.0.0.0 --port 8000`.
2. Builds the frontend image in three stages: `npm ci`, `next build` with
   `BACKEND_URL=http://backend:8000` baked in, then a runtime stage that runs `npm start`
   on port 3000.
3. Starts both containers; the backend mounts `./data` and `./storage` from the host so the
   database and uploads persist across restarts.

Useful variations:

| Goal | How |
|---|---|
| Different host ports | Set `FRONTEND_PORT` / `BACKEND_PORT` in `.env` (e.g. `3200` / `8200`). Containers still listen on 3000 / 8000 internally; `CORS_ORIGINS` for the backend is derived from `FRONTEND_PORT` by `docker-compose.yml`. |
| Run in the background | `docker compose up --build -d`; logs with `docker compose logs -f backend`. |
| Rebuild after a code change | `docker compose up --build` again (the images copy the source; there is no bind mount of code). |
| Stop | `docker compose down` (data and uploads stay in `./data` and `./storage`). |
| Production-like switches | `APP_ENV=production` (refuses the dev response cache), a long random `STORAGE_SECRET`, `OPENROUTER_REFERER` set to your origin. |

Caveats:

- The frontend's `/api` rewrite target is resolved at **build** time (`next build` bakes
  `rewrites()` into the routes manifest). If you point the frontend at a backend other than the
  compose service, rebuild the image with the right `BACKEND_URL` build environment — changing
  the runtime `environment:` alone has no effect.
- The backend is a **single process** by design (in-process task registry, SQLite). Do not run
  it with multiple uvicorn workers or several replicas against one database.

---

## 5. Route B — local development (backend and frontend on the host)

### 5.1 Backend

```bash
cd backend
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e ".[dev]"

cp ../.env.example ../.env        # once; then set OPENROUTER_API_KEY in ../.env
.venv/bin/uvicorn app.main:app --reload --port 8000
```

Notes:

- Settings are read from `.env` at the **repository root** first, then `backend/.env`
  (`app/config.py`); it does not matter which directory you launch from for settings, but it
  does for paths: the defaults `DB_PATH=./data/tprm.sqlite` and `STORAGE_DIR=./storage` are
  relative to the current working directory. Launched from `backend/` as above, the database and
  uploads land in `backend/data` and `backend/storage`. Set absolute paths in `.env` if you want
  them elsewhere (the Docker route uses `/app/data` and `/app/storage`).
- `--reload` restarts the process on code changes; any background job running at that moment is
  marked interrupted and the UI offers a re-run.
- Startup creates the schema, applies additive migrations, reconciles interrupted tasks and logs
  an error (without exiting) if a configured model fails the capability check (§ARCHITECTURE 4.1).

Verify:

```bash
curl -s http://localhost:8000/api/health
# {"ok":true,"app_env":"dev","llm_dev_cache":false}
.venv/bin/python scripts/smoke_openrouter.py    # one tiny call per profile against your real key
```

### 5.2 Frontend

In a second terminal:

```bash
cd frontend
npm ci
npm run dev            # http://localhost:3000, proxies /api to http://localhost:8000
```

`npm run dev` reads `BACKEND_URL` at start (default `http://localhost:8000`); set it if your
backend runs elsewhere, e.g. `BACKEND_URL=http://localhost:8200 npm run dev`. For a production
build on the host:

```bash
BACKEND_URL=http://localhost:8000 npm run build
npm start              # serves the build on :3000
```

Type-check without building: `npx tsc --noEmit -p tsconfig.json`.

### 5.3 First assessment (manual smoke test)

1. Open http://localhost:3000, click **Create**, name a vendor.
2. On **1. Scoping** type a two-sentence description, **Submit description**, then **Start
   scoping**; answer the question or **Force continue**.
3. **2. Inherent risk** → **Generate scenarios**.
4. **3. Evidence** → upload at least one PDF, XLSX or DOCX and choose its kind; wait for the
   per-document extraction to finish (the AI activity indicator shows the stages).
5. **4. Analysis** → run the three steps in order: cross-correlate, gap analysis, narratives &
   summary.
6. **5. Residual score** shows the matrix, the register and the cited evidence; **6. Report** is
   the printable version. Edit any control verdict in a scenario drawer and watch the band update.

Actions whose prerequisites are not met are disabled with the backend's reason next to them.

---

## 6. Route C — the benchmark harness

```bash
cd benchmark
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"

cat > .env <<'ENV'
OPENROUTER_API_KEY=sk-or-...
BENCH_BACKEND_URL=http://localhost:8000
JUDGE_MODEL=anthropic/claude-sonnet-4.6
MAIN_DB_PATH=/abs/path/to/backend/data/tprm.sqlite
ENV

.venv/bin/bench list-cases                       # validates every case.yaml
.venv/bin/bench run --smoke                      # cheap plumbing check against a running backend
.venv/bin/uvicorn dashboard.app:app --port 8100  # results dashboard at http://localhost:8100
```

The seven shipped cases (`benchmark/cases/*`) include their evidence documents, so a fresh clone
can run them. `benchmark/README.md` has the full CLI, configuration and grading reference, and
`docs/accuracy-program/TESTING.md` the cost-aware protocol for iterating on accuracy.

---

## 7. Running the tests

No network or API key is needed; the backend tests use a fake model.

```bash
# backend (194 tests, ≈ 40 s)
cd backend && .venv/bin/pytest -q

# frontend: type-check and production build
cd frontend && npx tsc --noEmit -p tsconfig.json && npm run build

# benchmark (73 tests, ≈ 4 s)
cd benchmark && .venv/bin/pytest -q
```

Check FTS5 support of your Python's SQLite:

```bash
python3 -c "import sqlite3; c=sqlite3.connect(':memory:'); c.execute('create virtual table t using fts5(x)'); print('fts5 ok', sqlite3.sqlite_version)"
```

---

## 8. Configuration reference

All variables live in `.env.example` with comments and defaults; the backend validates the
budget and liveness policies at startup and refuses inconsistent values.

| Group | Variables | Default | Notes |
|---|---|---|---|
| OpenRouter | `OPENROUTER_API_KEY` | – | **Required** for any AI call. |
| | `OPENROUTER_BASE_URL`, `OPENROUTER_REFERER`, `OPENROUTER_APP_NAME` | `https://openrouter.ai/api/v1`, `http://localhost:3000`, `cyber-tprm-assessment` | A proxy that strips usage accounting breaks cost capture. |
| Models | `MODEL_FAST`, `MODEL_REASONER` | `anthropic/claude-haiku-4.5`, `anthropic/claude-opus-4.7` | Any OpenRouter id; see the capability guard in ARCHITECTURE §4.1. |
| | `MODEL_FAST_ALTERNATIVES`, `MODEL_REASONER_ALTERNATIVES` | see `.env.example` | Comma-separated; offered per stage on the Settings page. |
| Storage | `DB_PATH`, `DATA_DIR`, `STORAGE_DIR`, `STORAGE_SECRET` | `./data/tprm.sqlite`, `./data`, `./storage`, `change-me…` | Relative to the working directory. Set a real secret before non-local use. |
| Ports and CORS | `FRONTEND_PORT`, `BACKEND_PORT` (Docker only), `CORS_ORIGINS` | 3000, 8000, `http://localhost:3000` | `CORS_ORIGINS` must match the browser-facing frontend origin when running outside Docker. |
| Tunables | `FTS_TOPK`, `MAX_UPLOAD_MB` | 8, 50 | Retrieval depth in fallback gap analysis; upload limit. |
| Output budgets | `LLM_BUDGET_SMALL/MEDIUM/LARGE`, `LLM_TRUNCATION_CAP`, `LLM_TRUNCATION_RETRIES`, `LLM_MIN_MODEL_OUTPUT_CAP` | 16384 / 32768 / 65536, 128000, 2, 128000 | Must be non-decreasing in that order. Budgets bound hidden reasoning too. Lowering any below default is an accuracy regression (`CLAUDE.md`). |
| Provider routing | `OPENROUTER_PROVIDER_IGNORE` | empty | Comma-separated OpenRouter provider names sent as `provider.ignore`; set `StreamLake` (as `.env.example` does) — its content filter truncates security/jurisdiction text. |
| LLM failure forensics | `LLM_FAILURE_DUMP_DIR`, `LOG_LEVEL` | unset, `INFO` | Failed calls are always summarised on `model_call` (`attempts_json`, `finish_reason`, `provider`, `generation_id`, `output_head/tail`) and logged at WARNING; the dump dir additionally stores every attempt's full output as JSON. |
| Liveness | `LLM_CONNECT_TIMEOUT_S`, `LLM_STREAM_IDLE_S`, `LLM_CONTENT_SILENCE_S`, `LLM_CALL_MAX_S`, `TASK_IDLE_TIMEOUT_S`, `TASK_MAX_RUNTIME_S` | 30, 180, 3600, 3600, 600, 14400 | Inactivity-based; see ARCHITECTURE §6. |
| Deployment | `APP_ENV`, `LLM_DEV_CACHE` | `dev`, `0` | The dev cache is refused when `APP_ENV=production`; the benchmark refuses to measure against a backend that has it on. |

---

## 9. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Frontend image build fails: `"/app/public": not found` | `frontend/public/` must exist (it holds only a `.gitkeep`). Restore it if it was deleted. |
| Every AI action returns 502 `OPENROUTER_API_KEY is not set` | The key is missing from the `.env` the backend actually read (repo root first, then `backend/.env`). |
| 502 `OpenRouter 402` or credits errors mid-stage | OpenRouter reserves credit for the full requested `max_tokens` per in-flight call; a ladder retry reserves up to `LLM_TRUNCATION_CAP` (128 000 by default, times the stage's concurrency). Top up rather than lowering budgets. |
| Backend log: `Configured reasoner model fails the capability check` (or `fast model`) | The configured model's catalogued output cap or context window is below its profile's requirement (reasoner ≥ `LLM_MIN_MODEL_OUTPUT_CAP` = 128 000 output tokens, fast ≥ `LLM_BUDGET_MEDIUM` = 32 768). The app still starts; the truncation ladder clamps at the model's own cap and fails loudly there. Pick a model that meets ARCHITECTURE §4.1. |
| A stage fails with `… failed validation after retry`, `… output truncated at max_tokens=…` or another LLM call error | Read the WARNING lines in the backend log and the `model_call` row for the call: `attempts_json` (per attempt: layer, requested budget, outcome, finish reasons, provider), `output_head` / `output_tail`. Set `LLM_FAILURE_DUMP_DIR` and re-run to capture the complete output of every attempt as JSON. |
| A stage fails with `… cut by the provider's content filter (… native_finish_reason=sensitive)` | OpenRouter routed the call to a host whose content filter stopped generation on security / data-residency text. Add that provider to `OPENROUTER_PROVIDER_IGNORE` (`.env.example` already excludes `StreamLake`) or switch models. |
| No `app.*` lines in the backend log | Root logging is configured from `LOG_LEVEL` (default `INFO`); uvicorn's `--log-level` covers only its own loggers. |
| Button disabled with "… is stale" / 409 responses | Sequential workflow enforcement: re-run the named upstream step. See ARCHITECTURE §2.1. |
| "Task lost on server restart — re-run the step" | The backend restarted (or `--reload` fired) while a job ran. Re-run; nothing is silently completed. |
| UI shows the API as unreachable while the backend is up | Frontend built or started with the wrong `BACKEND_URL`, or `CORS_ORIGINS` does not match the browser origin when the UI is served from another host/port. |
| Port already in use | Change `FRONTEND_PORT` / `BACKEND_PORT` (Docker) or the `--port` / `-p` flags (local). |
| A stage sits at "idle 15m" with no tokens | A model that reasons without streaming its reasoning looks silent; by default the silence tier is off and the call may take up to `LLM_CALL_MAX_S`. Cancel from the UI if needed. |
| `bench run` refuses the backend | Its `/api/health` reports `llm_dev_cache: true`; unset `LLM_DEV_CACHE` (or pass `--allow-dev-cache` for plumbing-only runs). |

---

## 10. Upgrading an existing installation

- Database: schema changes are additive and applied automatically at backend startup
  (`app/db.py`, `_ADDITIVE_COLUMNS`); there is no Alembic and no manual step. Back up `data/`
  and `storage/` before upgrading anyway.
- Frontend: run `npm ci` after pulling (the lock file may have changed) and rebuild.
- Docker: `docker compose up --build`.
- Model ids: OpenRouter retires models; a 404 with "deprecated" on any stage means the
  configured `MODEL_*` id (or a per-assessment override) needs updating.
