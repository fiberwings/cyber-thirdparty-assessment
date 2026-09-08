"""Guard against the frontend's hand-mirrored stage list drifting from the
model-override contract. The Settings page renders one row per stage from
frontend/lib/settings.ts; the keys must be exactly the ModelOverrides
fields and the fast/reasoner split must match patch_model_overrides."""

import re
from pathlib import Path

import pytest

from app.schemas.api import ModelOverrides

FRONTEND_SETTINGS = Path(__file__).resolve().parents[2] / "frontend" / "lib" / "settings.ts"
FAST_STAGES = {"scoping", "narrative"}  # app/api/assessments.py patch_model_overrides


def _frontend_stages() -> list[tuple[str, str]]:
    src = FRONTEND_SETTINGS.read_text()
    start = src.index("export const STAGES")
    end = src.index("];", start)
    return re.findall(r'key:\s*"(\w+)",\s*label:\s*"[^"]*",\s*profile:\s*"(fast|reasoner)"', src[start:end])


def test_frontend_stage_keys_match_model_overrides():
    if not FRONTEND_SETTINGS.exists():
        pytest.skip("frontend checkout not present")
    stages = _frontend_stages()
    assert [k for k, _ in stages] == list(ModelOverrides.model_fields)


def test_frontend_stage_profiles_match_backend_split():
    if not FRONTEND_SETTINGS.exists():
        pytest.skip("frontend checkout not present")
    stages = _frontend_stages()
    assert {k for k, p in stages if p == "fast"} == FAST_STAGES
    assert {k for k, p in stages if p == "reasoner"} == set(ModelOverrides.model_fields) - FAST_STAGES
