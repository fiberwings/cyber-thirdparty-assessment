from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.ai.router import OpenRouterError
from app.api import (
    assessments,
    document_weaknesses,
    documents,
    gap_analysis,
    models,
    report,
    scenarios,
    scoping,
    scoring,
    tasks,
    weaknesses,
)
from app.ai.router import warn_if_configured_models_undersized
from app.config import settings
from app.db import init_db
from app.tasks import reconcile_interrupted_tasks, registry


def _configure_logging() -> None:
    """Give the app's own loggers (app.ai.router retry/failure forensics,
    task watchdog) a handler and level. uvicorn configures only its own
    loggers, so without this `app.*` WARNINGs reach stderr via Python's bare
    last-resort handler and INFO is dropped. LOG_LEVEL (default INFO)."""
    import logging

    root = logging.getLogger()
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    if not root.handlers:
        logging.basicConfig(
            level=level,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
    else:
        root.setLevel(level)


_configure_logging()


@asynccontextmanager
async def lifespan(_: FastAPI):
    import asyncio

    init_db()
    reconcile_interrupted_tasks()
    warn_if_configured_models_undersized()
    # Liveness watchdog: cancels quiet / runaway jobs so an assessment is
    # never wedged behind a dead task (see app.tasks).
    watchdog = asyncio.create_task(registry.watchdog_loop())
    try:
        yield
    finally:
        watchdog.cancel()


app = FastAPI(
    title="Cyber TPRM Assessment",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(OpenRouterError)
async def _openrouter_error_handler(_: Request, exc: OpenRouterError) -> JSONResponse:
    status = 502
    return JSONResponse(
        status_code=status,
        content={
            "detail": str(exc),
            "upstream_code": exc.upstream_code,
            "transient": exc.transient,
        },
    )


@app.get("/api/health")
def health():
    # llm_dev_cache is exposed so a benchmark can refuse to measure against a
    # backend that would serve cached model responses.
    return {
        "ok": True,
        "app_env": settings.app_env,
        "llm_dev_cache": settings.llm_dev_cache_active,
    }


for router in (
    assessments.router,
    scoping.router,
    documents.router,
    document_weaknesses.router,
    scenarios.router,
    gap_analysis.router,
    weaknesses.router,
    scoring.router,
    report.router,
    models.router,
    tasks.router,
):
    app.include_router(router)
