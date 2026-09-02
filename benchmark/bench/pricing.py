"""OpenRouter list pricing, for costing judge calls recorded before metered
`usage.cost` was captured.

The benchmark never imports main-app code, so this mirrors (rather than shares)
`backend/scripts/backfill_cost.py`. An estimate is list price × recorded tokens:
it assumes pricing has not moved since the call and ignores any prompt-cache
discount, so it is an upper bound. Rows priced this way are labelled
`cost_source = "estimated"` and never overwrite a metered figure.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

MODELS_URL = "https://openrouter.ai/api/v1/models"


@dataclass(frozen=True)
class Price:
    prompt: float  # USD per input token
    completion: float  # USD per output token


def fetch_pricing(url: str = MODELS_URL) -> dict[str, Price]:
    resp = httpx.get(url, timeout=30)
    resp.raise_for_status()
    out: dict[str, Price] = {}
    for m in resp.json()["data"]:
        p = m.get("pricing") or {}
        try:
            out[m["id"]] = Price(float(p.get("prompt") or 0), float(p.get("completion") or 0))
        except (TypeError, ValueError):
            continue
    return out


def estimate(model_id: str, input_tokens: int | None, output_tokens: int | None,
             pricing: dict[str, Price]) -> float | None:
    """None when the model is not (or no longer) listed — an unknown price must
    not be recorded as $0."""
    price = pricing.get(model_id)
    if price is None:
        return None
    return (input_tokens or 0) * price.prompt + (output_tokens or 0) * price.completion
