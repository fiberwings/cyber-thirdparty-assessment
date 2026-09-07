"""Garbled-output handling in `call_structured`.

A degenerate response (keys that barely overlap the schema, or non-JSON
text) is retried once with the ORIGINAL messages — feeding the garbage back
as context keeps the model in the same mode. A coherent-but-invalid response
still takes the stricter schema retry. Neither path ever accepts partial
output; two garbled responses fail loudly.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from app.ai.router import OpenRouterError, _looks_garbled, call_structured
from app.db import SessionLocal
from app.models import ModelCall


class _Out(BaseModel):
    control_code: str
    coverage: str
    effectiveness: str
    rationale: str


MESSAGES = [{"role": "user", "content": "assess"}]
VALID = {"control_code": "X", "coverage": "full", "effectiveness": "strong", "rationale": "ok"}


def test_looks_garbled_classification():
    assert _looks_garbled('{"control_codevote": ": "}', {"control_codevote": ": "}, _Out)
    assert _looks_garbled("Net.Wq {{{", None, _Out)  # not JSON-shaped at all
    # Coherent JSON with a missing field is a schema miss, not garble.
    coherent = {"control_code": "X", "coverage": "full", "effectiveness": "strong"}
    assert not _looks_garbled('{"control_code": ...}', coherent, _Out)
    # A JSON object that merely failed to parse (e.g. trailing comma) is a schema miss.
    assert not _looks_garbled('{"control_code": "X",}', None, _Out)


def test_sparse_on_schema_answer_is_a_schema_miss_not_garble():
    """Mostly-optional schemas (the attestation profile) get sparse but
    coherent answers: a pen test states none of the SOC-only fields. Such an
    answer with a wrong field *shape* must reach the corrective retry — the
    old "≤ 1/3 of schema fields present" rule sent it down the garble path,
    which never shows the validator error and failed the profile outright."""
    from app.schemas.attestation import AttestationProfileOut

    sparse = {"doc_type": "pentest", "report_date": "2025-11-28", "scope": "Veltrix web app"}
    assert not _looks_garbled('{"doc_type": ...}', sparse, AttestationProfileOut)
    # Extra invented fields around recognised ones are a schema miss too
    # (minimax padded the pen-test profile with ~35 foreign fields).
    assert not _looks_garbled("{...}", {"control_code": "X", "zz": 1, "yy": 2, "xx": 3}, _Out)
    # A single foreign wrapper around an object is a shape miss, not garble.
    assert not _looks_garbled("{...}", {"result": {"control_code": "X"}}, _Out)
    # An empty object is garble.
    assert _looks_garbled("{}", {}, _Out)


async def _call(db, fake_client):
    return await call_structured(
        db, purpose="test", profile="fast", messages=MESSAGES, schema=_Out, client=fake_client
    )


@pytest.mark.asyncio
async def test_garbled_output_retries_with_fresh_context(fresh_db, fake_client):
    fake_client.push_json({"control_codevote": ": "})
    fake_client.push_json(VALID)
    with SessionLocal() as db:
        out = await _call(db, fake_client)
        assert out.control_code == "X"
        # The retry must NOT carry the garbage as an assistant turn.
        assert fake_client.calls[1]["messages"] == MESSAGES
        mc = db.query(ModelCall).order_by(ModelCall.id.desc()).first()
        assert mc.ok is True and mc.error == ""


@pytest.mark.asyncio
async def test_garbled_twice_fails_loudly(fresh_db, fake_client):
    fake_client.push_json({"control_codevote": ": "})
    fake_client.push("Net.Wq {{{ json")
    with SessionLocal() as db:
        with pytest.raises(OpenRouterError) as exc:
            await _call(db, fake_client)
        assert "garbled output twice" in str(exc.value)
        assert len(fake_client.calls) == 2
        mc = db.query(ModelCall).order_by(ModelCall.id.desc()).first()
        assert mc.ok is False and mc.error.startswith("garbled output twice")


@pytest.mark.asyncio
async def test_schema_miss_still_takes_stricter_retry(fresh_db, fake_client):
    missing = {k: v for k, v in VALID.items() if k != "rationale"}
    fake_client.push_json(missing)
    fake_client.push_json(VALID)
    with SessionLocal() as db:
        out = await _call(db, fake_client)
        assert out.rationale == "ok"
        msgs = fake_client.calls[1]["messages"]
        assert msgs[-2]["role"] == "assistant"  # bad output fed back
        assert "did not match the required JSON schema" in msgs[-1]["content"]


@pytest.mark.asyncio
async def test_garble_then_schema_miss_uses_both_budgets(fresh_db, fake_client):
    fake_client.push_json({"control_codevote": ": "})
    fake_client.push_json({k: v for k, v in VALID.items() if k != "rationale"})
    fake_client.push_json(VALID)
    with SessionLocal() as db:
        out = await _call(db, fake_client)
        assert out.rationale == "ok"
        assert len(fake_client.calls) == 3
        assert fake_client.calls[1]["messages"] == MESSAGES
        assert fake_client.calls[2]["messages"][-2]["role"] == "assistant"
