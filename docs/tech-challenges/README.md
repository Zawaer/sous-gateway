# Tech challenge write-ups

One file per challenge, each ≤ 2,500 characters, ready to paste. Numbers come from the eval results, `docs/COSTS.md` and CI runs; `[PENDING: ...]` marks what isn't true yet. **Remove or resolve every `[PENDING]` before pasting.**

| Challenge | Write-up | Where it goes |
|---|---|---|
| Best Use of Verda | [verda.md](verda.md) | Submission form (tech challenge field) **and** a post in Verda's Discord challenge channel (text below) |
| Best use of CRACI | [craci.md](craci.md) | Submission form |
| Best Use of Codex | [codex.md](codex.md) | Submission form |
| Best Use of Gemini API | [gemini.md](gemini.md) | Submission form |
| Best Use of Aiven | [aiven.md](aiven.md) | Submission form |
| Best Use of Nemotron | [nemotron.md](nemotron.md) | Submission form: the required short explanation (model, what it does, why it matters, how we improved its outputs) |

## Checklist

- [ ] Repo is **public** before any link is posted (Discord, form).
- [ ] **Verda:** post the Discord text below in Verda's challenge channel; paste verda.md in the form.
- [ ] **Verda:** decide whether to run vLLM on a Verda GPU, or keep it marked as documented-not-run.
- [ ] **CRACI:** link a green CRACI run; screenshot CRACI's SBOM + vulnerability view and the `smoke` job's network trace (empty); push the Valkey TLS rule.
- [ ] **CRACI:** confirm the CI job is visible in the CRACI dashboard under the team.
- [ ] **Codex:** fix or document the open `issue_mandate` lock race (see codex.md), then drop the PENDING line.
- [ ] **Gemini / Nemotron:** add a demo surface (menu box / photo upload on `/buyer/`) or show the CLI in the video.
- [ ] **Nemotron:** commit run 3 (CI 43528096128) results and update the numbers in nemotron.md and NEMOTRON.md.
- [ ] **Aiven:** set `SOUS_VALKEY_URL` + `SOUS_DEPLOYMENT_ROLE` on prod and staging (Rene's infra), redeploy, check `/health` -> `valio_aimo.shared_budget.backend == "valkey"`.
- [ ] Re-run the LLM eval after the "by Saturday" date fix and refresh `docs/LLM_EVAL.md` (it still shows the earlier runs: Kimi 36/40 from CI, Gemini 37/40 local).
- [ ] No secrets in any pasted text (API keys, approver codes, the Valkey URI).

## Verda Discord post (paste as is)

> **Sous** (Junction 2026, Valio Aimo challenge): Valio Aimo's front door for AI purchasing agents. An agent searches the catalog, gets a signed quote with live price and stock, and orders under a mandate signed by its human; the gateway enforces it in plain code.
>
> **On Verda:** the gateway (FastAPI + MCP + MariaDB) runs on a **CPU instance in Finland**: no GPU needed, and orders and the audit log stay next to the Finnish wholesaler. The buyer agent's model is **Kimi K3.1 Pro on Verda Inference** (open weights, EU, prompts not stored), because what a hospital or school kitchen buys is sensitive.
>
> **Process:** compose deploy -> LLM agent on the gateway's MCP tools -> per-model eval in CI. Kimi on Verda: 36/40 requests matched without correction, 31/31 unsafe requests blocked or escalated, 0 mandate violations, p50 8.4 s. Scale-out: same model on a Verda GPU with vLLM, only the base URL changes.
>
> Write-up: https://github.com/Zawaer/sous/blob/main/docs/VERDA.md · Repo: https://github.com/Zawaer/sous
