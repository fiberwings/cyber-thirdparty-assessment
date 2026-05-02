# Cyber Third-Party Risk Assessment

AI-assisted, fully cited TPRM workflow:

1. Describe the vendor service (AI loops Q&A until sufficient — or you force-continue).
2. AI proposes inherent risk scenarios + the controls each scenario expects.
3. Upload vendor evidence (questionnaire, SOC 2, ISO 27001, pen test, policies — PDF / XLSX / DOCX).
4. Gap analysis: every expected control is matched against retrieved evidence; coverage and effectiveness tracked separately, every claim cited with page/section + verbatim quote.
5. Weakness synthesizer surfaces emergent scenarios from pen-test findings or questionnaire negatives.
6. Deterministic scoring (4×4 impact × likelihood) with meta-issue uplift for vague answers and missing docs.
7. Edit any AI assessment → score recalculates instantly.

**Stack**: FastAPI + SQLAlchemy + SQLite/FTS5 (backend), Next.js 15 + Tailwind + TanStack Query (frontend), OpenRouter for interchangeable LLMs (Haiku for ingest/Q&A, Opus / GPT-5 for reasoning).

---

## Quick start (without Docker)

```bash
# 1. Backend
cd backend
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp ../.env.example ../.env  # edit OPENROUTER_API_KEY
.venv/bin/uvicorn app.main:app --reload --port 8000

# 2. Frontend (in a second terminal)
cd frontend
npm install
npm run dev   # http://localhost:3000
```

Open http://localhost:3000.

## Quick start (Docker)

```bash
cp .env.example .env       # edit OPENROUTER_API_KEY
docker compose up --build
```

## Tests

```bash
cd backend
.venv/bin/pytest             # unit + e2e (mocked LLM)
.venv/bin/python scripts/smoke_openrouter.py    # exercises a real OpenRouter key
```

The e2e test (`tests/test_e2e.py`) walks the full workflow end-to-end with a fake LLM that returns canned JSON — useful as an executable spec of the API surface.

## Architecture in 30 seconds

- **Citations are first-class.** AI structured output is validated by Pydantic and rejected if a control claimed `partial`/`full` coverage without ≥1 citation. Pipeline retries once with a stricter prompt; on second failure the control becomes `unknown` and a `meta_issue` is recorded.
- **Coverage vs. effectiveness are separate columns** so the UI can show "documented but weak in practice".
- **No LLM in the score path.** `app/scoring/engine.py` is pure Python. The model only writes the explanation prose *after* scoring.
- **Model routing is per-stage** and per-assessment overridable from the in-app `ModelPicker` (top right of every page).
- **Single-process task registry** powers the long-running scenario generation, gap analysis, and weakness synthesis steps. SSE endpoint at `/api/tasks/{id}/events`.

## Manual UI smoke test

1. Start backend and frontend (see Quick start).
2. Click **Create**, name a vendor.
3. Type a 2-sentence description, press **Submit description**.
4. Press **Start scoping** — the AI asks a question. Either answer it or click **Force continue**.
5. Click **2. Inherent risk** in the left nav, then **Generate scenarios**.
6. Click **3. Evidence** and upload at least one PDF or XLSX.
7. Click **4. Gap analysis** and run all three steps in order.
8. Click **5. Residual score** — every band, every cited piece of evidence is visible.
9. Open any scenario and edit a control's effectiveness from "strong" to "weak"; the band re-renders within a second.

## Project layout

```
backend/   — FastAPI + agents + scoring + parsing + tests
frontend/  — Next.js 15 (App Router) UI
storage/   — content-addressed uploads (gitignored)
data/      — SQLite DB (gitignored)
```

## Environment

See `.env.example`. Required: `OPENROUTER_API_KEY`. Everything else has sensible defaults for local use.
