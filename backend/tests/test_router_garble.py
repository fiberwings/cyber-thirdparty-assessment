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

from app.ai.router import LLMError, _looks_garbled, call_structured
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
        with pytest.raises(LLMError) as exc:
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


@pytest.mark.asyncio
async def test_failed_call_records_per_attempt_forensics(fresh_db, fake_client, caplog, tmp_path, monkeypatch):
    """A validation failure leaves enough on model_call (and in the dump dir)
    to explain *why*: per-attempt layer/outcome/finish reason and the output
    tail, plus a WARNING per retry and for the final failure."""
    import json as _json
    import logging

    from app.config import settings
    from app.models import ModelCall

    monkeypatch.setattr(settings, "llm_failure_dump_dir", tmp_path)
    torn = '{"answer": "starts fine but the body is torn' + "x" * 3000
    fake_client.push(torn)
    fake_client.push(torn)
    with caplog.at_level(logging.WARNING, logger="app.ai.router"):
        with SessionLocal() as db:
            with pytest.raises(LLMError):
                await call_structured(
                    db,
                    purpose="extract",
                    profile="fast",
                    messages=MESSAGES,
                    schema=_Out,
                    client=fake_client,
                )
    with SessionLocal() as db:
        mc = db.query(ModelCall).one()
    assert mc.ok is False
    assert mc.finish_reason == "stop"
    assert [a["layer"] for a in mc.attempts_json] == ["initial", "validation"]
    assert [a["outcome"] for a in mc.attempts_json] == ["invalid_json", "invalid_json"]
    assert mc.attempts_json[0]["max_tokens"] == settings.llm_budget_small
    assert mc.output_tail.endswith("xxx") and len(mc.output_tail) == 2000
    assert mc.output_head.startswith('{"answer"')
    msgs = [r.message for r in caplog.records]
    assert any("retrying" in m and "invalid_json" in m for m in msgs)
    assert any("FAILED after 2 attempt(s)" in m for m in msgs)
    dumps = list(tmp_path.glob("*_extract_a0_*.json"))
    assert len(dumps) == 1
    payload = _json.loads(dumps[0].read_text())
    assert len(payload["attempts"]) == 2 and payload["attempts"][1]["content"] == torn


@pytest.mark.asyncio
async def test_content_filtered_response_is_never_parsed(fresh_db, fake_client, monkeypatch):
    """A provider content-filter cut (native_finish_reason=sensitive, normalised
    to `stop`) that lands at a JSON-valid point must not pass as a complete
    answer: retry once with fresh context, then fail loudly naming the provider."""
    from app.models import ModelCall

    valid_but_cut = VALID  # coherent JSON — exactly the dangerous case
    fake_client.push_filtered(valid_but_cut, native="sensitive", provider="StreamLake")
    fake_client.push_json(VALID)
    with SessionLocal() as db:
        out = await call_structured(
            db, purpose="extract", profile="fast", messages=MESSAGES, schema=_Out, client=fake_client
        )
    assert out.control_code == "X"
    assert [c["messages"] for c in fake_client.calls] == [MESSAGES, MESSAGES]  # fresh context, not re-prompt
    with SessionLocal() as db:
        mc = db.query(ModelCall).one()
    assert [a["outcome"] for a in mc.attempts_json] == ["filtered", "ok"]
    assert mc.attempts_json[0]["provider"] == "StreamLake"

    fake_client.push_filtered(valid_but_cut, native="sensitive", provider="StreamLake")
    fake_client.push_filtered(valid_but_cut, native="sensitive", provider="StreamLake")
    with SessionLocal() as db:
        with pytest.raises(LLMError) as exc:
            await call_structured(
                db, purpose="extract", profile="fast", messages=MESSAGES, schema=_Out, client=fake_client
            )
    assert exc.value.filtered is True and exc.value.truncated is False
    assert "StreamLake" in str(exc.value) and "sensitive" in str(exc.value)
