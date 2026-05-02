from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import (
    assessments,
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


@app.get("/api/health")
def health():
    return {"ok": True}


for router in (
    assessments.router,
    scoping.router,
    documents.router,
    scenarios.router,
    gap_analysis.router,
    weaknesses.router,
    scoring.router,
    report.router,
    models.router,
    tasks.router,
):
    app.include_router(router)
