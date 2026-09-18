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
| OpenRouter API key | required for any AI call through OpenRouter | – | https://openrouter.ai. Keep a comfortable credit balance: OpenRouter reserves credit for each in-flight call's full `max_tokens`, and several stages run four calls in parallel. |
| Azure AI Foundry credentials | alternative to OpenRouter, per model ref | – | An Azure OpenAI / Foundry resource endpoint and API key (`AZURE_OPENAI_*`, `AZURE_INFERENCE_*`); see the settings table below. |
| Network access | to `openrouter.ai` and/or your Azure resource host from the backend | – | The frontend never talks to a model provider; the benchmark's judge does (OpenRouter). |

Platform: developed and verified on Linux (aarch64 and x86_64 both work since every dependency is
pure Python or ships wheels). macOS works the same way. Windows is untested; use WSL 2.

---

## 2. Repository layout

```
.
├── backend/            FastAPI app (Python) — API, AI pipeline, scoring, tests
│   ├── app/            package `app`
│   ├── scripts/        smoke_llm.py, backfill_cost.py
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
| httpx | ≥ 0.27 | 0.28.1 | Streaming client for the model providers |
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
.venv/bin/python scripts/smoke_llm.py           # one tiny call per profile through whichever provider its ref names
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
| OpenRouter | `OPENROUTER_API_KEY` | – | **Required** for any AI call routed through OpenRouter (bare model ids). |
| | `OPENROUTER_BASE_URL`, `OPENROUTER_REFERER`, `OPENROUTER_APP_NAME` | `https://openrouter.ai/api/v1`, `http://localhost:3000`, `cyber-tprm-assessment` | A proxy that strips usage accounting breaks cost capture. |
| Azure AI Foundry | `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_API_VERSION` | –, –, empty (v1 path) | For `azure:<deployment>` refs (Azure OpenAI deployments). Set the api-version only to force the legacy `/openai/deployments/…` path. |
| | `AZURE_INFERENCE_ENDPOINT`, `AZURE_INFERENCE_API_KEY`, `AZURE_INFERENCE_API_VERSION` | –, –, `2024-05-01-preview` | For `foundry:<deployment>` refs (Foundry Models endpoint, non-OpenAI models). |
| | `AZURE_DEPLOYMENT_META` | empty | `<deployment>=<canonical id>[;temp=fixed],…` — which catalogue model each deployment serves (required for profile defaults) and whether the temperature parameter must be omitted (explicit, warned at startup). |
| Models | `MODEL_FAST`, `MODEL_REASONER` | `anthropic/claude-haiku-4.5`, `anthropic/claude-opus-4.7` | Any OpenRouter id, or `azure:` / `foundry:` deployment ref; see the capability guard in ARCHITECTURE §4.1. |
| | `MODEL_FAST_ALTERNATIVES`, `MODEL_REASONER_ALTERNATIVES` | see `.env.example` | Comma-separated; offered per stage on the Settings page. |
| Storage | `DB_PATH`, `DATA_DIR`, `STORAGE_DIR`, `STORAGE_SECRET` | `./data/tprm.sqlite`, `./data`, `./storage`, `change-me…` | Relative to the working directory. Set a real secret before non-local use. |
| Ports and CORS | `FRONTEND_PORT`, `BACKEND_PORT` (Docker only), `CORS_ORIGINS` | 3000, 8000, `http://localhost:3000` | `CORS_ORIGINS` must match the browser-facing frontend origin when running outside Docker. |
| Tunables | `FTS_TOPK`, `MAX_UPLOAD_MB` | 8, 50 | Retrieval depth in fallback gap analysis; upload limit. |
| Output budgets | `LLM_BUDGET_SMALL/MEDIUM/LARGE`, `LLM_TRUNCATION_CAP`, `LLM_TRUNCATION_RETRIES`, `LLM_MIN_MODEL_OUTPUT_CAP` | 16384 / 32768 / 65536, 128000, 2, 128000 | Must be non-decreasing in that order. Budgets bound hidden reasoning too. Lowering any below default is an accuracy regression (`CLAUDE.md`). |
| Provider routing | `OPENROUTER_PROVIDER_IGNORE` | empty | Comma-separated OpenRouter provider names sent as `provider.ignore`; set `StreamLake` (as `.env.example` does) — its content filter truncates security/jurisdiction text. |
| LLM failure forensics | `LLM_FAILURE_DUMP_DIR`, `LOG_LEVEL` | unset, `INFO` | Failed calls are always summarised on `model_call` (`attempts_json`, `finish_reason`, `provider`, `generation_id`, `output_head/tail`) and logged at WARNING; the dump dir additionally stores every attempt's full output as JSON. |
| Liveness | `LLM_CONNECT_TIMEOUT_S`, `LLM_STREAM_IDLE_S`, `LLM_STREAM_IDLE_NO_KEEPALIVE_S`, `LLM_CONTENT_SILENCE_S`, `LLM_CALL_MAX_S`, `LLM_RETRY_AFTER_CAP_S`, `TASK_IDLE_TIMEOUT_S`, `TASK_MAX_RUNTIME_S` | 30, 180, = silence, 3600, 3600, 60, 600, 14400 | Inactivity-based; the no-keepalive tier applies to Azure streams; see ARCHITECTURE §6. |
| Deployment | `APP_ENV`, `LLM_DEV_CACHE` | `dev`, `0` | The dev cache is refused when `APP_ENV=production`; the benchmark refuses to measure against a backend that has it on. |

### 8.1 Running on Azure AI Foundry

The app talks to Azure through the **chat-completions** API of an Azure OpenAI resource (or the
Azure OpenAI surface of a Foundry resource). Everything above the wire — truncation ladder,
content-filter detection, retries, forensics, liveness — is the same code as for OpenRouter
(ARCHITECTURE §4.1). What has to exist on the Azure side, in order:

1. **A resource with a model deployment.** Creating the resource is not enough: the chat endpoint
   answers `404 DeploymentNotFound` until a *deployment* is added. In the Foundry portal
   (ai.azure.com) open the resource → **Models + endpoints** / **Deployments** → **Deploy model →
   Deploy base model**; in the Azure portal the same page is *Resource Management → Model
   deployments → Manage Deployments*. Note the **deployment name** — that (not the model name) is
   what goes after `azure:`.
2. **Which model.** The reasoner profile requires a catalogue entry with ≥ 200k context and a
   ≥ `LLM_MIN_MODEL_OUTPUT_CAP` (128k) output cap, so the GPT-5 family (`gpt-5`, `gpt-5-mini`,
   `gpt-5.4-mini`, …) qualifies and `gpt-4o` (16k output) is refused at startup. Verified live:
   `gpt-4o` for the wiring, `gpt-5-mini` and `gpt-5.4-mini` for full benchmark runs.
   Deployment type *Global Standard* is fine (no regional pinning is required by the app; pick
   *Data Zone* / *Standard* if residency matters to you).
3. **Tokens-per-minute quota — the setting that actually decides whether a run completes.**
   Azure admits a request by reserving its `max_completion_tokens` against the deployment's TPM,
   and the reasoner stages request `LLM_BUDGET_LARGE` (65 536) per call with up to 4 calls in
   flight (gap analysis, scenario detailing). A 100k-TPM deployment therefore rejects the second
   concurrent gap-analysis call with 429 — the retry loop cannot fix that, and the run ends with
   unassessed controls (`failed_targets`; the benchmark marks such a run invalid). Set the TPM
   slider to **≥ 300k for the reasoner deployment; 500k was verified to run orbitclear with zero
   429s**. If the quota tab caps you lower, request a quota increase or point only the fast
   profile at Azure. A 429 storm is diagnosable after the fact: the WARNING and
   `attempts_json[].transport_retries` carry the `x-ratelimit-limit-tokens` header next to the
   reserved budget.
4. **Content-filter policy.** Azure's filter is stricter than most OpenRouter providers and is
   configured per deployment. Assessment evidence is security text (pen-test findings, incident
   descriptions) and can trip *violence* / *self-harm* categories at medium severity; the router
   never parses a cut completion, so a filtered call is a lost call
   (`native_finish_reason=content_filter:<category>/<severity>` on the `model_call` row). If that
   happens, create a custom content filter with a higher threshold for that deployment
   (*Safety + security → Content filters* in Foundry) — it needs the *Modified content filters*
   approval on some subscriptions — and attach it to the deployment.
5. **Endpoint and key.** The portal shows the full sample URL (e.g.
   `https://<resource>.openai.azure.com/openai/deployments/<name>/chat/completions?api-version=…`
   or `…/openai/responses`); the app wants only the **host**:
   `AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com` (a Foundry resource's
   `https://<resource>.services.ai.azure.com` works too). The key is *Keys and Endpoint* → KEY 1.
   Leave `AZURE_OPENAI_API_VERSION` empty: the app uses the versionless `/openai/v1/` path; set
   it only if your resource still requires the legacy `/deployments/…?api-version=` path (older
   sovereign clouds).
6. **App configuration** (`.env`, or the environment of the process):

   ```
   AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com
   AZURE_OPENAI_API_KEY=<key>
   AZURE_DEPLOYMENT_META=gpt-5-mini=openai/gpt-5-mini;temp=fixed
   MODEL_REASONER=azure:gpt-5-mini        # fast profile can stay on OpenRouter
   ```

   `AZURE_DEPLOYMENT_META` maps the deployment to a `MODEL_CAPS` entry (the deployment name says
   nothing about the model, and the truncation ladder needs the output cap) — a profile default
   without an entry is a startup ERROR. `temp=fixed` is required for reasoning deployments
   (GPT-5 family, o-series), which reject the profile's `temperature=0.2` with a 400; it omits
   the parameter, is warned at startup and recorded per attempt as `temperature_sent: null`. A
   model missing from `MODEL_CAPS` (`backend/app/ai/router.py`) only warns; add it with the
   context window and output cap from the Azure model page before running assessments on it.
7. **Verify** with `backend/scripts/smoke_llm.py` (one tiny call per profile, prints dialect,
   provider label `azure-openai:<resource>`, finish reason and usage), then run one assessment
   and check the `model_call` rows: `cost_usd = 0` / `cost_source = ""` is expected (Azure does
   not meter cost; tokens are exact), and every attempt should be `ok`.

Things to know: Azure streams send no keepalive comments, so a reasoning deployment that thinks
for minutes is silent on the socket — the `LLM_STREAM_IDLE_NO_KEEPALIVE_S` tier and a watchdog
heartbeat cover that (do not lower it). Deployments propagate for up to ~5 minutes after
creation. The `foundry:<deployment>` scheme (Foundry Models inference endpoint for DeepSeek /
Llama / Mistral / Phi, `AZURE_INFERENCE_*`) is implemented and unit-tested against the
documented wire format but has **not** been exercised against a live Foundry Models endpoint
yet; an OpenAI-family deployment on a Foundry resource should use `azure:` regardless.

---

## 9. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Frontend image build fails: `"/app/public": not found` | `frontend/public/` must exist (it holds only a `.gitkeep`). Restore it if it was deleted. |
| Every AI action returns 502 `OPENROUTER_API_KEY is not set` (or `AZURE_OPENAI_API_KEY` / `AZURE_INFERENCE_ENDPOINT` …) | The credential the model ref's provider needs is missing from the `.env` the backend actually read (repo root first, then `backend/.env`). |
| Backend log: `Configured reasoner model: azure:… has no AZURE_DEPLOYMENT_META entry` | Declare which catalogue model the deployment serves (`AZURE_DEPLOYMENT_META=<deployment>=<canonical id>`), otherwise the truncation ladder and capability guard cannot size it. |
| 502 `azure-openai 400: … 'temperature' does not support 0.2 …` | The deployment (o-series / GPT-5 reasoning) only accepts its default temperature. Add `;temp=fixed` to its `AZURE_DEPLOYMENT_META` entry — an explicit sampling change, recorded per attempt. |
| 502 `OpenRouter 402` or credits errors mid-stage | OpenRouter reserves credit for the full requested `max_tokens` per in-flight call; a ladder retry reserves up to `LLM_TRUNCATION_CAP` (128 000 by default, times the stage's concurrency). Top up rather than lowering budgets. |
| Backend log: `Configured reasoner model fails the capability check` (or `fast model`) | The configured model's catalogued output cap or context window is below its profile's requirement (reasoner ≥ `LLM_MIN_MODEL_OUTPUT_CAP` = 128 000 output tokens, fast ≥ `LLM_BUDGET_MEDIUM` = 32 768). The app still starts; the truncation ladder clamps at the model's own cap and fails loudly there. Pick a model that meets ARCHITECTURE §4.1. |
| A stage fails with `… failed validation after retry`, `… output truncated at max_tokens=…` or another LLM call error | Read the WARNING lines in the backend log and the `model_call` row for the call: `attempts_json` (per attempt: layer, requested budget, outcome, finish reasons, provider), `output_head` / `output_tail`. Set `LLM_FAILURE_DUMP_DIR` and re-run to capture the complete output of every attempt as JSON. |
| A stage fails with `… cut by the provider's content filter (… native_finish_reason=sensitive)` | OpenRouter routed the call to a host whose content filter stopped generation on security / data-residency text. Add that provider to `OPENROUTER_PROVIDER_IGNORE` (`.env.example` already excludes `StreamLake`) or switch models. |
| … `(azure-openai:… native_finish_reason=content_filter:violence/medium)` or `azure-openai rejected the prompt: content filter` | The Azure deployment's content-filter policy stopped the completion (or rejected the prompt). Relax the policy for that deployment in Azure AI Foundry or use another deployment; the router never parses a cut body. |
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
