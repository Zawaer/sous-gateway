# What Sous spent

_Generated from [evals/costs.yaml](../evals/costs.yaml) by `scripts/costs.py`, prorated to Sat 14.11 02:15 (Helsinki)._ Every paid model or API run and every running service is in the ledger; "unknown" means the price still has to be read from that provider's console.

## Totals per provider (model usage)

| Provider | Runs | Tokens in / out | Cost |
|---|---|---|---|
| gemini | 4 | 2,803,371 / 40,460 | $4.51 |
| openai | 1 | 356,000 / 2,400 | unknown |
| verda | 5 | 1,705,389 / 46,785 | $3.88 |

**Known total so far: $8.92** (plus the items marked unknown)

## Services (prorated)

| Provider | What | Since | Running | Cost so far | Note |
|---|---|---|---|---|---|
| aiven | Valkey, Developer plan (shared Valio budget) | 2026-11-13 22:30 | 3.8 h | $0.08 | paid from Aiven's $50 trial credits |
| verda | CPU instance CPU.4V.16G, prod (FIN-01) | 2026-11-13 21:15 | 5.0 h | $0.24 | Verda on-demand price from the instance record |
| verda | CPU instance CPU.4V.16G, staging (FIN-01) | 2026-11-13 21:40 | 4.6 h | $0.22 | Verda on-demand price from the instance record |
| craci | CI runners (test, docker, smoke, eval, llm-eval) | 2026-11-13 21:40 | 4.6 h | unknown | paid from CRACI's per-team compute credits; see the CRACI dashboard |

## Every model run

| When | Provider | Model | Purpose | Tokens in / out | Cost |
|---|---|---|---|---|---|
| 2026-11-13 21:40 | verda | kimi-k3-1-pro | buyer LLM eval, 40 cases (CI on CRACI, run 43505798752) | 0 / 0 | $0.00 | key rejected (401), no tokens used
| 2026-11-13 23:40 | verda | kimi-k3-1-pro | buyer LLM eval, 40 cases (CI on CRACI, run 43511463514) | 849,421 / 20,386 | $1.90 |
| 2026-11-13 23:44 | gemini | gemini-4-hydrogen | buyer LLM eval, 40 cases (Rene, local run) | 935,045 / 12,292 | $1.49 |
| 2026-11-14 01:19 | gemini | gemini-4-hydrogen | buyer LLM eval, 40 cases (CI on CRACI, run 43524836008) | 926,590 / 12,178 | $1.48 |
| 2026-11-14 01:26 | verda | kimi-k3-1-pro | buyer LLM eval, 40 cases (CI on CRACI, run 43524836008) | 848,184 / 20,939 | $1.91 |
| 2026-11-14 01:30 | gemini | gemini-4-hydrogen | recipe eval, 7 menus (CI run 43524836008) | 10,172 / 3,934 | $0.04 |
| 2026-11-14 01:30 | verda | kimi-k3-1-pro | recipe eval, 7 menus (CI run 43524836008) | 7,784 / 5,460 | $0.07 |
| 2026-11-14 02:04 | gemini | gemini-4-hydrogen | buyer LLM eval, 40 cases (CI on CRACI, run 43530717095) | 931,564 / 12,056 | $1.49 |
| 2026-11-14 02:04 | verda | kimi-k3-1-pro | buyer LLM eval, 40 cases (CI on CRACI, run 43530717095) | 0 / 0 | $0.00 | Verda answered HTTP 200 with an empty body; every case failed before using tokens
| 2026-11-14 00:10 | openai | codex | read-only security review of the trust core (Codex run 01a1d6fd) | 356,000 / 2,400 | unknown | approximate, from the Codex run summary

## Notes

- Gemini and Verda keys are shared with Rene's other work until the Sous-only keys land; usage before the switch is attributed from these run logs, not from the provider dashboards.
- Valio Aimo's API is free for the challenge (rate-limited, not billed).
- Valio Aimo API calls are counted separately per request in the eval results (they cost nothing but rate limit).
