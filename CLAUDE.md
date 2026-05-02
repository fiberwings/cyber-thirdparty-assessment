# Cyber TPRM Assessment

## Prime directive: assessment accuracy

The product is a cyber risk assessment that humans rely on. **Accuracy outranks DX, speed, and code elegance.** Before any change, ask: *could this make the assessment less faithful to the evidence?* If yes, surface it in chat as **"Accuracy trade-off:"** with the risk and an alternative, and wait for confirmation before implementing.

Common accuracy regressions to watch for:
- Lowering `max_tokens`, swapping models, or relaxing temperature on the reasoner profile
- Validation/retry logic that masks bad model output instead of surfacing it
- Truncating service descriptions, evidence chunks, or the control catalogue to save tokens
- Defaults / fallbacks that silently downgrade scoring when inputs are missing
- Caching or dedup that lets stale evidence stand in for fresh evidence
- Prompt edits that drop "cite the evidence" / "say unknown when unknown" guardrails

When in doubt, flag and ask — a paused turn is cheap; a wrong assessment is not.
