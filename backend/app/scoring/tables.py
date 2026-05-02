"""4x4 risk matrix lookups.

Levels are 1..4 corresponding to (Low, Moderate, High, VeryHigh).
RISK_MATRIX[impact-1][likelihood-1] returns the band name.

The matrix is conservative-leaning: anything with VeryHigh impact starts at
Moderate even at Low likelihood, because rare but catastrophic events still
demand attention.
"""

from __future__ import annotations

# rows = impact 1..4, cols = likelihood 1..4
RISK_MATRIX: list[list[str]] = [
    # L=1         L=2          L=3         L=4
    ["Low",      "Low",        "Moderate", "Moderate"],   # I=1 Low
    ["Low",      "Moderate",   "Moderate", "High"],       # I=2 Moderate
    ["Moderate", "Moderate",   "High",     "VeryHigh"],   # I=3 High
    ["Moderate", "High",       "VeryHigh", "VeryHigh"],   # I=4 VeryHigh
]

BAND_RANK = {"Low": 1, "Moderate": 2, "High": 3, "VeryHigh": 4}
RANK_BAND = {v: k for k, v in BAND_RANK.items()}
