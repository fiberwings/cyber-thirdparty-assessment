# Cyber TPRM Assessment

## Prime directive: assessment accuracy

The product is a cyber risk assessment that humans rely on. **Accuracy outranks DX, speed, and code elegance.** Before any change, ask: *could this make the assessment less accurate, or faithful to the evidence?* If yes, surface it in chat as **"Accuracy trade-off:"** with the risk and an alternative, and wait for confirmation before implementing.

Common accuracy regressions to watch for:
- Lowering `max_tokens` budgets (they live in `backend/app/config.py` as `LLM_BUDGET_*` /
  `LLM_TRUNCATION_*`; *raising* them is accuracy-positive, lowering any below the shipped
  defaults requires the "Accuracy trade-off:" protocol), swapping models, or relaxing
  temperature on the reasoner profile
- Bypassing the split-on-truncate contract: a `finish_reason == "length"` response must
  never be parsed or persisted, and callers rely on the loud `truncated=True` failure to
  subdivide oversized batches — never paper over it with silent budget inflation
- Validation/retry logic that masks bad model output instead of surfacing it
- Truncating service descriptions, evidence chunks, or the control catalogue to save tokens
- Defaults / fallbacks that silently downgrade scoring when inputs are missing
- Caching or dedup that lets stale evidence stand in for fresh evidence
- Prompt edits that drop "cite the evidence" / "say unknown when unknown" guardrails
- Lowering the liveness limits (`LLM_CONTENT_SILENCE_S`, `LLM_CALL_MAX_S`, `TASK_IDLE_TIMEOUT_S`,
  `TASK_MAX_RUNTIME_S` in `backend/app/config.py`) below the shipped defaults: completions are
  streamed and deadlines are inactivity-based precisely so slow reasoning models finish instead of
  being cut off; tightening them re-introduces speed-based failures (a timed-out stage is a lost
  assessment step, never a faster one)

When in doubt, flag and ask — a paused turn is cheap; a wrong assessment is not.

## Documentation map

- `README.md` — product overview and quick start.
- `docs/ARCHITECTURE.md` — phases, AI usage, large-file handling, scoring; keep it in sync with code changes.
- `docs/BUILD.md` — dependencies, versions, build-from-source steps.
- `benchmark/README.md` — accuracy benchmark harness.
- `docs/accuracy-program/STATE.md` — living log of the accuracy programme; read first when resuming that work.
