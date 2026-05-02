from __future__ import annotations

from fastapi import APIRouter

from app.ai.router import get_profiles
from app.schemas.api import ModelProfileRead

router = APIRouter(prefix="/api/models", tags=["models"])


@router.get("", response_model=list[ModelProfileRead])
def list_profiles():
    return [
        ModelProfileRead(name=p.name, default_model=p.default_model, alternatives=p.alternatives)
        for p in get_profiles().values()
    ]
