# What Sous spent

_Generated from [evals/costs.yaml](../evals/costs.yaml) by `scripts/costs.py`, prorated to Sat 14.11 18:21 (Helsinki)._ Every paid model or API run and every running service is in the ledger; "unknown" means the price still has to be read from that provider's console.

## Totals per provider (model usage)

| Provider | Runs | Tokens in / out | Cost |
|---|---|---|---|
| gemini | 15 | 8,287,787 / 121,033 | $13.34 |
| nvidia | 3 | 5,013,242 / 64,986 | $0.00 |
| openai | 2 | 728,700 / 4,734 | unknown |
| verda | 16 | 9,061,014 / 178,632 | $32.67 |

**Known total so far: $48.42** (plus the items marked unknown)

## Services (prorated)

| Provider | What | Since | Running | Cost so far | Note |
|---|---|---|---|---|---|
| aiven | Valkey, Developer plan (shared Valio budget) | 2026-11-13 22:30 | 19.9 h | $0.41 | paid from Aiven's $50 trial credits |
| verda | CPU instance CPU.4V.16G, prod (FIN-01) | 2026-11-13 21:15 | 21.1 h | $1.01 | Verda on-demand price from the instance record |
| verda | CPU instance CPU.4V.16G, staging (FIN-01) | 2026-11-13 21:40 | 20.7 h | $0.99 | Verda on-demand price from the instance record |
| craci | CI runners (test, docker, smoke, eval, llm-eval) | 2026-11-13 21:40 | 20.7 h | unknown | paid from CRACI's per-team compute credits; see the CRACI dashboard |

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
| 2026-11-14 02:31 | gemini | gemini-4-hydrogen | buyer LLM eval, 51 cases / 43 run (CI push on CRACI, run 43533142451, 768efee) | 1,178,762 / 13,443 | $1.87 |
| 2026-11-14 02:38 | verda | kimi-k3-1-pro | buyer LLM eval, 51 cases / 43 run (CI push on CRACI, run 43533142451, 768efee) | 1,073,314 / 21,598 | $2.36 |
| 2026-11-14 03:33 | gemini | gemini-4-hydrogen | buyer LLM eval, 59 cases / 49 run (CI push on CRACI, run 43539740484, a4644a0) | 1,324,375 / 15,330 | $2.10 |
| 2026-11-14 03:40 | verda | kimi-k3-1-pro | buyer LLM eval, 59 cases / 49 run (CI push on CRACI, run 43539740484, a4644a0) | 1,205,877 / 27,178 | $2.68 |
| 2026-11-14 06:50 | gemini | gemini-4-hydrogen | buyer LLM eval, 59 cases / 49 run (CI push on CRACI, run 43557209740, 0b0ad68) | 1,379,854 / 15,075 | $2.18 |
| 2026-11-14 06:57 | verda | kimi-k3-1-pro | buyer LLM eval, 59 cases / 49 run (CI push on CRACI, run 43557209740, 0b0ad68) | 1,265,705 / 26,299 | $2.79 |
| 2026-11-14 07:45 | gemini | gemini-4-hydrogen | mandate drafting eval, 19 cases, 15/19 (CI on CRACI, run 43562850006, 1f65871) | 26,519 / 2,260 | $0.06 |
| 2026-11-14 07:58 | gemini | gemini-4-hydrogen | mandate drafting eval, 19 cases, 19/19 after the validator + prompt fixes (CI on CRACI, run 43563950578, 5c44284) | 32,694 / 2,327 | $0.07 |
| 2026-11-14 08:23 | gemini | gemini-4-hydrogen | mandate drafting eval, 23 cases, 23/23 with QA's never-approve / allowed-allergen / preference cases (CI on CRACI, run 43566010358, d158f4c) | 41,773 / 2,808 | $0.08 |
| 2026-11-14 11:28 | gemini | gemini-4-hydrogen | mandate drafting eval, 23 cases, 23/23 (CI dispatch on CRACI, run 43580577498, 3972163) | 41,773 / 2,523 | $0.08 |
| 2026-11-14 11:40 | nvidia | nemotron-3-super-120b-a12b | buyer LLM eval, llm_only dispatch (CI on CRACI, run 43581555078, 81bebbd) | 0 / 0 | $0.00 | model id answered HTTP 404 to our key; every case failed before using tokens
| 2026-11-14 12:39 | nvidia | nemotron-3.5-super-vl-120b-a12b | buyer LLM eval, 60 cases / 48 run, 30/48 (CI llm_only dispatch on CRACI, run 43585403494, 04fb521) | 2,766,623 / 35,398 | $0.00 |
| 2026-11-14 12:59 | gemini | gemini-4-hydrogen | mandate drafting eval, 23 cases, 23/23 (CI full dispatch on CRACI, run 43587725624, 11d8448) | 41,773 / 2,808 | $0.08 |
| 2026-11-14 13:16 | gemini | gemini-4-hydrogen | buyer LLM eval, 60 cases / 48 run, 46/48 (CI full dispatch on CRACI, run 43587725624, 11d8448) | 1,396,549 / 15,557 | $2.21 |
| 2026-11-14 13:24 | verda | kimi-k3-1-pro | buyer LLM eval, 60 cases / 48 run, 47/48 (CI full dispatch on CRACI, run 43587725624, 11d8448) | 1,267,474 / 26,833 | $2.80 |
| 2026-11-14 13:33 | nvidia | nemotron-3.5-super-vl-120b-a12b | buyer LLM eval, 60 cases / 48 run, 30/48 (CI full dispatch on CRACI, run 43587725624, 11d8448) | 2,246,619 / 29,588 | $0.00 |
| 2026-11-14 13:40 | gemini | gemini-4-hydrogen | recipe eval, 7 menus, 5/7 (CI run 43587725624, 11d8448) | 10,172 / 4,304 | $0.05 |
| 2026-11-14 13:40 | verda | kimi-k3-1-pro | recipe eval, 7 menus, 4/7 (CI run 43587725624, 11d8448) | 7,784 / 5,056 | $0.07 |
| 2026-11-14 14:46 | gemini | gemini-4-hydrogen | recipe eval, 7 menus, 5/7 (CI recipe_only dispatch, run 43595658113, da3e797) | 10,172 / 4,138 | $0.05 |
| 2026-11-14 14:46 | verda | kimi-k3-1-pro | recipe eval, 7 menus, 5/7 (CI recipe_only dispatch, run 43595658113, da3e797) | 7,784 / 6,106 | $0.08 |
| 2026-11-14 00:10 | openai | codex | read-only security review of the trust core (Codex run 01a1d6fd) | 356,000 / 2,400 | unknown | approximate, from the Codex run summary
| 2026-11-14 04:50 | openai | codex | read-only security review of overnight trust changes (Codex run 01a1d86a) | 372,700 / 2,334 | unknown | approximate, from the Codex run summary
| 2026-11-14 12:38 | verda | gpu-a100-80gb | red team: 2 A100 80GB boxes that never booted (same dead IP), deleted by infra | 0 / 0 | $1.30 | from rene-claude-infra's instance records
| 2026-11-14 13:37 | verda | gpu-rtx-pro-6000-96gb | red team: Qwen3-30B-A3B (vLLM) on 1x RTX PRO 6000 96GB, 2,000 requests, 83.0M tokens in / 0.79M out (docs/REDTEAM.md); 2.35 h x $2.89 | 0 / 0 | $6.79 | self-hosted: tokens cost GPU time, not per token; Gemma 4 setup attempts included
| 2026-11-14 15:20 | verda | gpu-a100-80gb | red team: Gemma 4 26B-A4B-it (vLLM 0.33) on 1x A100 80GB FIN-02, 600 requests + 100 slicing re-run after the fix, 35.6M tokens in / 0.30M out (docs/REDTEAM.md); 1.12 h x $2.39 | 0 / 0 | $2.68 | self-hosted: GPU time, not per token
| 2026-11-14 15:58 | verda | qwen3-30b-a3b-selfhosted | buyer LLM eval, 48 cases / 47 run, provider verda-selfhosted (our vLLM on sous-redteam-gpu3; CI run 43612368619) | 2,527,687 / 18,777 | $0.00 | self-hosted: no per-token charge; the GPU is billed as the service line below
| 2026-11-14 16:17 | verda | gpu-rtx-pro-6000-96gb | sous-redteam-gpu3 kept as the self-hosted model endpoint (vLLM 0.11 + Caddy, provider verda-selfhosted), 13:47-16:17Z, then deleted | 0 / 0 | $7.23 | 2.5 h x $2.893 from rene-claude-infra's instance record (deleted 16:17Z; whole box 4.9 h = $14.2 incl. the red-team line above); disk kept for Sunday, storage not included

## Notes

- Google issued one Gemini key per team and the Verda account is shared across the team's projects, so provider dashboards can't be split per project: every Sous figure here is computed from our own logged tokens and run ids.
- Valio Aimo's API is free for the challenge (rate-limited, not billed).
- Valio Aimo API calls are counted separately per request in the eval results (they cost nothing but rate limit).
