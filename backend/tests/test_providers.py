"""Provider dialect registry: model refs, deployment metadata, credential and
startup checks, and the config validator for the no-keepalive read timeout."""

from __future__ import annotations

import logging

import pytest

from app.ai.providers import (
    check_provider_config,
    parse_deployment_meta,
    parse_model_ref,
    ref_problem,
)
from app.ai.router import (
    MODEL_CAPS,
    _ladder_ceiling,
    validate_model_capability,
    warn_if_configured_models_undersized,
)
from app.config import Settings, settings


@pytest.fixture()
def azure_env(monkeypatch):
    monkeypatch.setattr(settings, "azure_openai_endpoint", "https://acme-eu.openai.azure.com")
    monkeypatch.setattr(settings, "azure_openai_api_key", "ak")
    monkeypatch.setattr(settings, "azure_inference_endpoint", "https://acme-eu.services.ai.azure.com")
    monkeypatch.setattr(settings, "azure_inference_api_key", "fk")
    monkeypatch.setattr(settings, "azure_deployment_meta", "gpt5-prod=openai/gpt-5;temp=fixed,haiku-eu=anthropic/claude-haiku-4.5")


# ---------------------------------------------------------------- model refs


@pytest.mark.parametrize(
    "raw, scheme, wire, canonical",
    [
        ("anthropic/claude-opus-4.7", "openrouter", "anthropic/claude-opus-4.7", "anthropic/claude-opus-4.7"),
        ("deepseek/deepseek-r1:free", "openrouter", "deepseek/deepseek-r1:free", "deepseek/deepseek-r1:free"),
        ("openrouter:z-ai/glm-5.1", "openrouter", "z-ai/glm-5.1", "z-ai/glm-5.1"),
        ("azure:unmapped-dep", "azure", "unmapped-dep", "unmapped-dep"),
        ("foundry:deepseek-v4", "foundry", "deepseek-v4", "deepseek-v4"),
    ],
)
def test_parse_model_ref_matrix(raw, scheme, wire, canonical):
    ref = parse_model_ref(raw)
    assert (ref.scheme, ref.wire, ref.canonical, ref.fixed_temperature) == (scheme, wire, canonical, False)
    assert ref.raw == raw


def test_parse_model_ref_uses_deployment_meta(azure_env):
    ref = parse_model_ref("azure:gpt5-prod")
    assert ref.canonical == "openai/gpt-5" and ref.fixed_temperature is True
    ref = parse_model_ref("azure:haiku-eu")
    assert ref.canonical == "anthropic/claude-haiku-4.5" and ref.fixed_temperature is False


@pytest.mark.parametrize("raw", ["bogus:x", "azure:", "", "   "])
def test_parse_model_ref_rejects_bad_refs(raw):
    with pytest.raises(ValueError):
        parse_model_ref(raw)


def test_deployment_meta_parsing():
    meta = parse_deployment_meta(" gpt5-prod = openai/gpt-5 ; temp=fixed , haiku-eu=anthropic/claude-haiku-4.5 ")
    assert meta["gpt5-prod"].canonical == "openai/gpt-5" and meta["gpt5-prod"].fixed_temperature
    assert meta["haiku-eu"].canonical == "anthropic/claude-haiku-4.5" and not meta["haiku-eu"].fixed_temperature
    assert parse_deployment_meta("") == {}
    with pytest.raises(ValueError):
        parse_deployment_meta("gpt5-prod")  # no canonical
    with pytest.raises(ValueError):
        parse_deployment_meta("gpt5-prod=openai/gpt-5;temp=hot")  # unknown flag


# ---------------------------------------------------------------- credentials


def test_ref_problem_names_the_missing_setting(monkeypatch):
    monkeypatch.setattr(settings, "azure_openai_endpoint", "")
    monkeypatch.setattr(settings, "azure_openai_api_key", "")
    assert "AZURE_OPENAI_ENDPOINT" in ref_problem("azure:x")
    monkeypatch.setattr(settings, "azure_openai_endpoint", "https://acme.openai.azure.com")
    assert "AZURE_OPENAI_API_KEY" in ref_problem("azure:x")
    monkeypatch.setattr(settings, "azure_inference_endpoint", "")
    assert "AZURE_INFERENCE_ENDPOINT" in ref_problem("foundry:x")
    assert ref_problem("anthropic/claude-opus-4.7") is None  # conftest sets OPENROUTER_API_KEY
    assert "scheme" in ref_problem("bogus:x")


def test_check_provider_config_requires_meta_for_azure_defaults(azure_env):
    assert check_provider_config(["azure:gpt5-prod", "anthropic/claude-opus-4.7"], defaults=True) == []
    problems = check_provider_config(["azure:unmapped"], defaults=True)
    assert len(problems) == 1 and "AZURE_DEPLOYMENT_META" in problems[0]
    assert check_provider_config(["azure:unmapped"], defaults=False) == []


# ---------------------------------------------------------------- capability guard via canonical


def test_capability_guard_resolves_azure_deployment_to_canonical(azure_env, caplog):
    assert validate_model_capability("azure:gpt5-prod", "reasoner") is None
    reason = validate_model_capability("azure:haiku-eu", "reasoner")
    assert reason is not None and "output tokens" in reason and "azure:haiku-eu" in reason
    with caplog.at_level("WARNING"):
        assert validate_model_capability("azure:unmapped", "reasoner") is None
    assert any("capability table" in r.message and "azure:unmapped" in r.message for r in caplog.records)


def test_ladder_ceiling_uses_the_canonical_cap(azure_env):
    assert _ladder_ceiling("azure:gpt5-prod") == min(settings.llm_truncation_cap, MODEL_CAPS["openai/gpt-5"][1])
    assert _ladder_ceiling("azure:haiku-eu") == MODEL_CAPS["anthropic/claude-haiku-4.5"][1]
    assert _ladder_ceiling("azure:unmapped") == settings.llm_truncation_cap


def test_capability_guard_rejects_unroutable_refs(monkeypatch):
    monkeypatch.setattr(settings, "azure_openai_api_key", "")
    monkeypatch.setattr(settings, "azure_openai_endpoint", "https://acme.openai.azure.com")
    reason = validate_model_capability("azure:x", "reasoner")
    assert reason is not None and "AZURE_OPENAI_API_KEY" in reason
    assert "scheme" in validate_model_capability("bogus:x", "fast")


# ---------------------------------------------------------------- startup checks


def test_startup_logs_missing_credentials_and_meta_as_errors(monkeypatch, caplog):
    monkeypatch.setattr(settings, "azure_openai_endpoint", "")
    monkeypatch.setattr(settings, "azure_openai_api_key", "")
    monkeypatch.setattr(settings, "azure_deployment_meta", "")
    monkeypatch.setattr(settings, "model_reasoner", "azure:gpt5-prod")
    with caplog.at_level(logging.WARNING):
        warn_if_configured_models_undersized()
    errors = [r.message for r in caplog.records if r.levelno == logging.ERROR]
    assert [m for m in errors if "AZURE_OPENAI_ENDPOINT" in m] and len(errors) == 1  # reported once


def test_startup_errors_on_azure_default_without_meta_and_warns_temp_fixed(azure_env, monkeypatch, caplog):
    monkeypatch.setattr(settings, "model_reasoner", "azure:unmapped")
    monkeypatch.setattr(settings, "model_fast_alternatives", "azure:also-unmapped")
    with caplog.at_level(logging.WARNING):
        warn_if_configured_models_undersized()
    errors = [r.message for r in caplog.records if r.levelno == logging.ERROR]
    warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("AZURE_DEPLOYMENT_META" in m and "azure:unmapped" in m for m in errors)
    assert not any("also-unmapped" in m for m in errors)  # alternatives are not errors
    assert any("gpt5-prod" in m and "temp=fixed" in m for m in warnings)


def test_startup_is_quiet_for_openrouter_only_config(caplog):
    with caplog.at_level(logging.WARNING):
        warn_if_configured_models_undersized()
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


# ---------------------------------------------------------------- config validator


def test_no_keepalive_timeout_defaults_to_content_silence_and_is_bounded():
    s = Settings(_env_file=None)
    assert s.stream_idle_no_keepalive_s == s.llm_content_silence_s
    assert Settings(_env_file=None, LLM_STREAM_IDLE_NO_KEEPALIVE_S=900).stream_idle_no_keepalive_s == 900
    with pytest.raises(ValueError):
        Settings(_env_file=None, LLM_STREAM_IDLE_NO_KEEPALIVE_S=10)  # below LLM_STREAM_IDLE_S
    with pytest.raises(ValueError):
        Settings(_env_file=None, LLM_STREAM_IDLE_NO_KEEPALIVE_S=7200)  # above LLM_CALL_MAX_S
    with pytest.raises(ValueError):
        Settings(_env_file=None, LLM_RETRY_AFTER_CAP_S=-1)
