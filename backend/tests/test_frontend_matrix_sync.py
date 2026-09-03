"""Guard against the frontend's hand-mirrored risk matrix drifting from the
scoring engine's table. The UI renders empty matrix cells from its own copy
(frontend/lib/riskMatrix.ts) because scenarios only carry their own band."""

import re
from pathlib import Path

import pytest

from app.scoring.tables import RISK_MATRIX

FRONTEND_MATRIX = Path(__file__).resolve().parents[2] / "frontend" / "lib" / "riskMatrix.ts"


def test_frontend_risk_matrix_matches_backend():
    if not FRONTEND_MATRIX.exists():
        pytest.skip("frontend checkout not present")
    src = FRONTEND_MATRIX.read_text()
    start = src.index("RISK_MATRIX")
    end = src.index("];", start)
    frontend = re.findall(r'"(Low|Moderate|High|VeryHigh)"', src[start:end])
    backend = [band for row in RISK_MATRIX for band in row]
    assert frontend == backend
