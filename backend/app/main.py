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
from app.config import settings
from app.db import init_db


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


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
    return {"ok": True}


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
