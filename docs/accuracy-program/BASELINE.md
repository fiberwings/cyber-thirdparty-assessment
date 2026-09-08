# Baseline — app @ d391c80, benchmark run 5 (+ manual completions), 2026-08-29
Models: fast minimax/minimax-m3, reasoner deepseek/deepseek-v4-pro-0813, judge z-ai/glm-5.1. One repetition. Full detail in
`testdata/_results/` (git-ignored: the evaluation records and the vendor evidence packs are not in the repository).

| case | gaps | found | judge P | signal share | dups | boilerplate | misread | app band | expected | LLM calls | in tok | out tok | gap-analysis wall |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| cloudnimbus | 3 | 3 (1 partial) | 7.5 % | 10 % | 2 | 20 | 14 | VeryHigh | Moderate | 152 | 707,515 | 449,558 | 24 min |
| quantedge | 6 | 5 | 12.5 % | 48 % | 5 | 14 | 2 | VeryHigh | Moderate | 139 | 571,179 | 413,467 | 19 min |
| verifypro | 8 | 8 | 7.7 % | 58 % | 15 | 17 | 12 | VeryHigh | High | 176 | 919,692 | 703,235 | 31 min |
| globaltalent | 12 | 12 | 9.8 % | 64 % | 22 | 21 | 1 | VeryHigh | VeryHigh | 253* | 1,071,560 | 682,495 | 40 min |
| meridian | 2 (+6 opt) | 2 (+4) | 3.7 % | 38 % | 8 | 19 | 9 | VeryHigh | Moderate | 134 | 581,973 | 443,674 | 24 min |
| orbitclear (canary) | 5 (+3 opt) | 5 (1 partial) (+1) | 12.8 % | 35 % | 4 | 19 | 3 | VeryHigh | High | 251* | 1,044,630 | 764,057 | 21 min |
\* includes retries / re-runs. Scenario-level: weakness uplift at cap in 65/72 scenarios; residual likelihood > inherent in 61/72.

Canary reference (orbitclear, single clean pipeline ≈ 130 calls, ≈ 0.55 M in / 0.4 M out tokens, ≈ 35 min end-to-end).
Stored assessments for LLM-free scoring work: `backend/data/tprm.sqlite` ids 6 cloudnimbus, 7 globaltalent, 8 meridian,
9 orbitclear, 10 quantedge, 11 verifypro (do not delete).
