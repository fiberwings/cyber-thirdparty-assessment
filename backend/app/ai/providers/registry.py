"""Model refs → dialects.

    anthropic/claude-opus-4.7      OpenRouter (bare id; `openrouter:` also accepted)
    azure:gpt5-prod                Azure OpenAI deployment
    foundry:deepseek-v4            Foundry Models deployment

`AZURE_DEPLOYMENT_META` maps a deployment to the catalogue id it serves (so
the capability guard and the truncation ladder know its window and output
cap) and carries the explicit `temp=fixed` flag. A deployment without an
entry keeps its own name as `canonical`, which the guard treats like any
uncatalogued id (warn, don't reject) — except for a profile *default*, where
it is a startup ERROR because the ladder ceiling depends on it.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable

from app.config import settings

from .azure import AzureFoundryDialect, AzureOpenAIDialect
from .base import Dialect, ModelRef
from .openrouter import OpenRouterDialect

SCHEMES = ("openrouter", "azure", "foundry")
_AZURE_SCHEMES = ("azure", "foundry")


@dataclass(frozen=True)
class DeploymentMeta:
    canonical: str
    fixed_temperature: bool = False


def parse_deployment_meta(raw: str) -> dict[str, DeploymentMeta]:
    """`gpt5-prod=openai/gpt-5;temp=fixed,haiku-eu=anthropic/claude-haiku-4.5`."""
    out: dict[str, DeploymentMeta] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if "=" not in entry:
            raise ValueError(
                f"AZURE_DEPLOYMENT_META entry {entry!r} must be <deployment>=<canonical>[;temp=fixed]"
            )
        name, rest = entry.split("=", 1)
        parts = [p.strip() for p in rest.split(";")]
        canonical = parts[0]
        if not name.strip() or not canonical:
            raise ValueError(f"AZURE_DEPLOYMENT_META entry {entry!r} has an empty deployment or canonical id")
        fixed = False
        for flag in parts[1:]:
            if flag == "temp=fixed":
                fixed = True
            elif flag:
                raise ValueError(f"AZURE_DEPLOYMENT_META entry {entry!r}: unknown flag {flag!r}")
        out[name.strip()] = DeploymentMeta(canonical=canonical, fixed_temperature=fixed)
    return out


def deployment_meta() -> dict[str, DeploymentMeta]:
    return parse_deployment_meta(settings.azure_deployment_meta)


def parse_model_ref(raw: str) -> ModelRef:
    """Parse a configured model string. Raises ValueError on an unknown scheme."""
    s = (raw or "").strip()
    if not s:
        raise ValueError("model ref is empty")
    scheme, sep, rest = s.partition(":")
    if not sep or "/" in scheme or scheme not in SCHEMES:
        # No scheme (OpenRouter ids are `vendor/model`, never `word:`).
        if sep and "/" not in scheme:
            raise ValueError(
                f"unknown model provider scheme {scheme!r} in {raw!r} "
                f"(expected one of: {', '.join(SCHEMES)}, or a bare OpenRouter id)"
            )
        return ModelRef(raw=s, scheme="openrouter", wire=s, canonical=s)
    rest = rest.strip()
    if not rest:
        raise ValueError(f"model ref {raw!r} names no model/deployment after {scheme}:")
    if scheme == "openrouter":
        return ModelRef(raw=s, scheme="openrouter", wire=rest, canonical=rest)
    meta = deployment_meta().get(rest)
    return ModelRef(
        raw=s,
        scheme=scheme,
        wire=rest,
        canonical=meta.canonical if meta else rest,
        fixed_temperature=meta.fixed_temperature if meta else False,
    )


def make_dialects(api_key: str | None = None, base_url: str | None = None) -> dict[str, Dialect]:
    """One dialect per scheme. `api_key`/`base_url` override the OpenRouter
    settings (tests); the Azure dialects always read settings."""
    return {
        "openrouter": OpenRouterDialect(api_key=api_key, base_url=base_url),
        "azure": AzureOpenAIDialect(),
        "foundry": AzureFoundryDialect(),
    }


@lru_cache(maxsize=1)
def _default_dialects() -> dict[str, Dialect]:
    return make_dialects()


def dialect_for(ref: ModelRef) -> Dialect:
    return _default_dialects()[ref.scheme]


def ref_problem(raw: str) -> str | None:
    """Why `raw` cannot be used at all (bad scheme / missing credentials), or
    None. Capability (window / output cap) is the router guard's business."""
    try:
        ref = parse_model_ref(raw)
    except ValueError as e:
        return str(e)
    return dialect_for(ref).credentials_missing()


def check_provider_config(model_ids: Iterable[str], *, defaults: bool) -> list[str]:
    """Startup diagnostics for a set of configured refs. For profile
    *defaults* an Azure deployment without AZURE_DEPLOYMENT_META is an error
    (the ladder ceiling and the capability guard depend on the canonical id);
    for alternatives it is only informational."""
    problems: list[str] = []
    for raw in model_ids:
        problem = ref_problem(raw)
        if problem:
            problems.append(f"{raw}: {problem}")
            continue
        ref = parse_model_ref(raw)
        if defaults and ref.scheme in _AZURE_SCHEMES and ref.wire not in deployment_meta():
            problems.append(
                f"{raw}: no AZURE_DEPLOYMENT_META entry maps deployment {ref.wire!r} to a "
                "catalogue id, so its context window and output cap cannot be verified"
            )
    return problems
